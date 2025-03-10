#   Copyright (c) 2020 Room 525 Research Group, Zhejiang University.
#   All Rights Reserved.

import logging

import paddle.fluid as fluid
import paddle.fluid.io as io
import paddle.fluid.transpiler.distribute_transpiler as dist_transpiler
from paddle.fluid.executor import Executor
from paddle.fluid.parallel_executor import ParallelExecutor
from paddle.fluid.compiler import CompiledProgram
from paddle.fluid.framework import Program

from ...openKS_distributed.base import \
BaseDistributedAlgorithm, BaseDistributedOptimizer, Mode

from paddle.fluid import compiler
from .fs_wrapper import LocalFS, BDFS

import os
import sys
import six
import json
import re
import shutil

class TrainStatus(object):
    def __init__(self, epoch_no=-1):
        # completed epoch
        self._epoch_no = epoch_no

    def next(self):
        return self._epoch_no + 1

    def __eq__(self, t):
        return self._epoch_no == t._epoch_no

    def __ne__(self, t):
        return not self == t


class GPUDistributedAlgorithm(BaseDistributedAlgorithm):
    def __init__(self):
        super(GPUDistributedAlgorithm, self).__init__(Mode.COLLECTIVE)
        self._local_ip = 0

        self.startup_program = None
        self._origin_program = None
        self._transpiled_program = None
        self.main_program = None
        self._checkoint_prefix = "__paddle_fleet_checkpoint__"
        self._param_file_name = "_paddle_fleet_param__"

    def init_worker(self):
        logging.warn(
            "You should not call 'init_worker' method for collective mode.")

    def run_worker(self, main_programs=None, scopes=None):
        logging.warn(
            "You should not call 'run_worker' method for collective mode.")

    def init_server(self, model_dir=None):
        logging.warn(
            "You should not call 'init_server' method for collective mode.")

    def run_server(self):
        logging.warn(
            "You should not call 'run_server' method for collective mode.")

    def stop_worker(self):
        logging.warn(
            "You should not call 'stop_worker' method for collective mode.")

    def distributed_optimizer(self, optimizer, strategy=None):
        self._optimizer = \
            HeterogeneousDistributedOptimizer(optimizer, strategy)
        return self._optimizer

    def save_inference_model(self,
                             executor,
                             dirname,
                             feeded_var_names=None,
                             target_vars=None,
                             main_program=None,
                             export_for_deployment=True):
        """
        Prune the given `main_program` to build a new program especially for
        inference, and then save it and all related parameters to given
        `dirname` by the `executor`.
        """
        assert isinstance(executor, Executor), \
            "In fleet.save_inference_model() function, executor must be as" \
            " Executor type."

        if main_program is None:
            main_program = self._origin_program
        assert isinstance(main_program, Program), \
            "In fleet.save_inference_model() function, main_program " \
            "must be as Program type."

        io.save_inference_model(dirname, feeded_var_names, target_vars,
                                executor, main_program, None, None,
                                export_for_deployment)

    def save_persistables(self,
                          executor,
                          dirname,
                          main_program=None,
                          filename=None):
        """
        This function filters out all variables with `persistable==True` from
        the give `main_program` and then saves these variables to the folder
        `dirname` or file `filename`.

        The `dirname` is used to specify the folder where persistable variables
        are going to be saved. If you would like to save variables in separate
        files, set `filename` None; if you would like to save all variables in a
        single file, use `filename` to specify the file name.
        """
        assert isinstance(executor, Executor), \
            "In fleet.save_inference_model() function, executor must be as" \
            " Executor type."

        if main_program is None:
            main_program = self._origin_program

        assert isinstance(main_program, Program), \
            "In fleet.save_inference_model() function, main_program " \
            "must be as Program type."

        io.save_persistables(executor, dirname, main_program, filename=filename)

    def _save_train_status(self, path, train_status):
        d = {}
        d["epoch_no"] = train_status._epoch_no

        file_name = "{}/fleet_train_status".format(path)
        with open(file_name, 'w') as f:
            json.dump(d, f)

    def _load_train_status(self, path):
        file_name = "{}/fleet_train_status".format(path)

        r = TrainStatus()
        if not os.path.isfile(file_name):
            return r

        d = {}
        with open(file_name, 'r') as f:
            d = json.load(f)

        assert "epoch_no" in d, "Can't find epoch_no in dict from train_status file:{}".format(
            d)
        r._epoch_no = d["epoch_no"]
        assert r._epoch_no >= 0, "Data in checkpoint file is not valid:{}".format(
            d)

        return r

    def _get_last_checkpoint_no(self, root_path, fs):
        """
        only get the first depth
        """
        max_no = -1
        d = {}
        dirs = fs.list_dirs(root_path)
        for dir in dirs:
            g = dir.split(".")
            if len(g) != 2:
                continue

            if g[0] != "__paddle_fleet_checkpoint__":
                continue

            try:
                n = int(g[1])
                if n > max_no:
                    max_no = n
            except:
                continue

        return max_no

    def clean_redundant_check_points(self,
                                     root_path,
                                     fs=LocalFS(),
                                     checkpoint_num=1):
        max_no = self._get_last_checkpoint_no(root_path, fs)
        if max_no < 0:
            return

        if checkpoint_num < 1:
            checkpoint_num = 1

        dirs = fs.list_dirs(root_path)
        for dir in dirs:
            g = dir.split(".")
            if len(g) != 2:
                continue

            if g[0] != self._checkoint_prefix:
                continue

            try:
                n = int(g[1])
                if n <= max_no - checkpoint_num:
                    path = "{}/{}.{}".format(root_path, self._checkoint_prefix,
                                             n)
                    fs.rmr(path)
            except Exception as e:
                print(e)
                continue

    def save_check_point(self,
                         executor,
                         path,
                         train_status,
                         main_program=None,
                         fs=LocalFS(),
                         local_cache_path=".cache",
                         remain_all_checkpoint=True):
        """
        This function save persistables and current epoch num to path.
        """

        if main_program == None:
            main_program = self._transpiled_program

        if not fs.stat(path):
            fs.mkdir(path)

        max_no = self._get_last_checkpoint_no(path, fs=fs)
        if max_no < 0:
            max_no = -1

        real_path = "{}/{}.{}".format(path, self._checkoint_prefix, max_no + 1)
        tmp_path = "{}.tmp".format(real_path)
        saved_path = tmp_path

        local_fs = LocalFS()

        cache_path = None
        if fs.need_upload_download():
            cache_path = "{}/{}.{}.saved_cache".format(
                local_cache_path, self._checkoint_prefix, max_no + 1)
            if not local_fs.stat(cache_path):
                local_fs.mkdir(cache_path)
            saved_path = cache_path

        self.save_persistables(
            executor=executor,
            dirname=saved_path,
            main_program=main_program,
            filename=self._param_file_name)
        self._save_train_status(path=saved_path, train_status=train_status)

        if fs.need_upload_download():
            fs.delete(tmp_path)
            fs.upload(cache_path, tmp_path)
        fs.mv(tmp_path, real_path)

        if not remain_all_checkpoint:
            self.clean_redundant_check_points(path)

    def load_check_point(self,
                         executor,
                         path,
                         trainer_id,
                         main_program=None,
                         fs=LocalFS(),
                         local_cache_path=".cache",
                         ignore_empty=True):
        """
        This function load persistables and current epoch num from path.
        """
        max_no = self._get_last_checkpoint_no(path, fs)

        if not ignore_empty:
            assert max_no >= 0, "Can't find checkpoint"

        if max_no < 0:
            return None

        local_fs = LocalFS()
        if fs.need_upload_download():
            cache_path = "{}/{}.{}.load_cache.{}".format(
                local_cache_path, self._checkoint_prefix, max_no, trainer_id)
            if local_fs.stat(cache_path):
                local_fs.delete(cache_path)

        real_path = "{}/{}.{}".format(path, self._checkoint_prefix, max_no)
        load_path = real_path
        if fs.need_upload_download():
            fs.download(real_path, cache_path)
            load_path = cache_path

        if main_program == None:
            main_program = self._transpiled_program

        io.load_persistables(
            executor=executor,
            dirname=load_path,
            main_program=main_program,
            filename=self._param_file_name)

        return self._load_train_status(load_path)

algorithm = GPUDistributedAlgorithm()
class HeterogeneousDistributedOptimizer(BaseDistributedOptimizer):
    """
    HeterogeneousDistributedOptimizer is a wrapper for paddle.fluid.optimizer
    HeterogeneousA user should pass a paddle.fluid.optimizer to DistributedOptimizer
    minimize() function is implemented.
    HeterogeneousDistributedOptimizer is the starting point for a user who wants to
    run distributed training. The optimized information will be stored in
    Fleet() instance who holds the global information about current distributed
    training.
    """

    def __init__(self, optimizer, strategy):
        super(HeterogeneousDistributedOptimizer, self).__init__(optimizer, strategy)
        self._forward_recompute = strategy.forward_recompute
        if (not isinstance(strategy.recompute_checkpoints, list)):
            raise ValueError("DistStrategy.recompute_checkpoints should"
                             "be a List")
        self._recompute_checkpoints = strategy.recompute_checkpoints
        self._use_amp = strategy.use_amp
        self._amp_loss_scaling = strategy.amp_loss_scaling
        self.print_config = False

    def backward(self,
                 loss,
                 startup_program=None,
                 parameter_list=None,
                 no_grad_set=None,
                 callbacks=None):
        return self._optimizer.backward(loss, startup_program, parameter_list,
                                        no_grad_set, callbacks)

    def apply_gradients(self, params_grads):
        return self._optimizer.apply_gradients(params_grads)

    def _check_condition(self, name, **kwargs):
        for k, v in six.iteritems(kwargs):
            if v is True:
                assert False, "you can't use %s and %s together" % (name, k)

    def _check_collective_mode(self, main_program, optimizer, strategy):
        """
        Check the conflict conditions.
        """
        if strategy.use_local_sgd:
            strategy.mode = "collective"
            strategy.collective_mode = "local_sgd"
            self._check_condition(
                "use_local_sgd",
                use_dgc=main_program._enable_dgc,
                use_dist_fc=strategy.use_dist_fc,
                use_lamb=main_program._use_lamb)

        if strategy.use_dist_fc:
            self._check_condition(
                "use_dist_fc",
                use_dgc=main_program._enable_dgc,
                use_local_sgd=strategy.use_local_sgd,
                use_lamb=main_program._use_lamb)
            assert strategy.dist_fc_config is not None, "GPUBuildStrategy.dist_fc_config should be set"

        if strategy._ut4grad_allreduce:
            strategy.mode = "collective"
            strategy.collective_mode = "grad_allreduce"
            self._check_condition(
                "_ut4grad_allreduce",
                use_dgc=main_program._enable_dgc,
                use_lamb=main_program._use_lamb)

        if self._strategy.collective_mode=="local_sgd" \
                or self._strategy.collective_mode == "grad_allreduce":
            assert self._strategy.mode == "collective", \
                "local_sgd and grad_allreduce can be used under collective mode"

    def _transpile(self, startup_program, main_program):
        """
        Transpile the programs to distributed programs. And add the variables.
        """
        worker_endpoints = algorithm.worker_endpoints()
        trainer_id = algorithm.worker_index()
        current_endpoint = algorithm.worker_endpoints()[trainer_id]
        worker_endpoints_env = ','.join(worker_endpoints)
        trainers_num = algorithm.worker_num()

        if self.print_config:
            print("worker_endpoints:{} trainers_num:{} current_endpoint:{} \
                  trainer_id:{}".format(worker_endpoints, trainers_num,
                                        current_endpoint, trainer_id))

        # call transpiler
        config = dist_transpiler.DistributeTranspilerConfig()
        config.mode = self._strategy.mode
        config.collective_mode = self._strategy.collective_mode

        config.nccl_comm_num = self._strategy.nccl_comm_num
        config.use_hierarchical_allreduce = self._strategy.use_hierarchical_allreduce
        config.hierarchical_allreduce_inter_nranks = self._strategy.hierarchical_allreduce_inter_nranks

        t = dist_transpiler.DistributeTranspiler(config=config)
        t.transpile(
            trainer_id=trainer_id,
            trainers=worker_endpoints_env,
            startup_program=startup_program,
            program=main_program,
            current_endpoint=current_endpoint)

    def _get_node_ips_from_endpoints(self, endpoints):
        ss = set()
        ips = []
        for ep in endpoints:
            ip = ep.split(":")[0].strip()
            if ip not in ss:
                ss.add(ip)
                ips.append(ip)
            else:
                continue

        return ips

    def _node_num(self):
        worker_endpoints = algorithm.worker_endpoints()
        current_endpoint = algorithm.worker_endpoints()[algorithm.worker_index()]
        worker_endpoints_env = ','.join(worker_endpoints)

        node_ips = self._get_node_ips_from_endpoints(worker_endpoints)
        node_ip = current_endpoint.split(":")[0].strip()

        node_num = len(node_ips)

        return node_num

    def _try_to_compile(self, startup_program, main_program):
        node_num = self._node_num()
        assert node_num >= 1, "nccl2 node_num must >= 1, now:{}" % node_num

        exec_strategy = self._strategy.exec_strategy

        if node_num <= 1:
            if self._strategy.nccl_comm_num > 1:
                logging.warn("set nccl_comm_num=1 since you only have 1 node.")
            self._strategy.nccl_comm_num = 1

            if self._strategy.use_hierarchical_allreduce:
                logging.warn(
                    "set use_hierarchical_allreduce=False since you only have 1 node."
                )
            self._strategy.use_hierarchical_allreduce = False

        sync_allreduce = os.getenv("FLAGS_sync_nccl_allreduce")
        if sync_allreduce is None or sync_allreduce == "1":
            exec_strategy.num_threads = self._strategy.nccl_comm_num + 1
            if self._strategy.use_hierarchical_allreduce:
                exec_strategy.num_threads = 2 * self._strategy.nccl_comm_num + 1
            if exec_strategy.num_threads > 4:
                logging.warn(
                    "if you use use_hierarchical_allreduce or "
                    "with multi nccl comm, please export FLAGS_sync_nccl_allreduce = 0"
                )

        # NOTE. open sync_batch_norm will hang when use multi num_threads
        sync_batch_norm = self._strategy.sync_batch_norm
        if sync_batch_norm is not None and sync_batch_norm is True:
            self._strategy.nccl_comm_num = 1
            self._strategy.use_hierarchical_allreduce = False
            exec_strategy.num_threads = 1
            logging.warn(
                "use sync_batch_norm will hang when set num_threads > 1, so "
                "set num_threads=1, nccl_comm_num=1, use_hierarchical_allreduce=False."
            )

        if self.print_config:
            print("node_num:", node_num, "num_threads:",
                  exec_strategy.num_threads, "use_hierarchical_allreduce:",
                  self._strategy.use_hierarchical_allreduce, "nccl_comm_num:",
                  self._strategy.nccl_comm_num, "FLAGS_sync_nccl_allreduce:",
                  sync_allreduce)

        self._transpile(startup_program, main_program)

        if self._strategy.mode == "collective":
            return main_program

        self._strategy.num_trainers = algorithm.worker_num()
        self._strategy.trainer_id = algorithm.worker_index()
        self._strategy.trainers_endpoints = algorithm.worker_endpoints()
        self._strategy.enable_backward_optimizer_op_deps = True

        self._compiled_program = compiler.CompiledProgram(main_program)

        self._compiled_program.with_data_parallel(
            loss_name=self._loss.name,
            build_strategy=self._strategy,
            exec_strategy=self._strategy.exec_strategy,
            share_vars_from=None)

        return self._compiled_program

    def raiseOptimizeError(self, strategy_name, optimize_name):
        raise ValueError("can not use {0} when you set DistStrategy.{1} "
                         "as True".format(optimize_name, strategy_name))

    def minimize(self,
                 loss,
                 startup_program=None,
                 parameter_list=None,
                 no_grad_set=None):
        """
        minimize a program through loss
        Args:
            loss (Variable|Variable List): loss variable or loss variable list to run optimization.
            startup_program (Program): startup_program for initializing parameters
                in `parameter_list`.
            parameter_list (list): list of Variables to update.
            no_grad_set (set|None): set of Variables should be ignored.
        Returns:
            tuple: (optimize_ops, params_grads) which are, list of operators appended;
            and list of (param, grad) Variables pair for optimization.
        Note that in parameter server mode, a worker will not get anything about optimize_os
        Because optimizer algorithms run on pserver side. We will make this usable in pserver
        process, but currently the optimization part is written into BaseDistributedAlgorithm(). A user does not
        need to care about how to startup a pserver node.
        """

        # check optimizer conflicts
        if self._forward_recompute:
            if self._recompute_checkpoints == []:
                raise ValueError("please set strategy.recompute_checkpoints"
                                 "when set strategy.forward_recompute as True")
            if self._optimizer.__class__.__name__ in [
                    "RecomputeOptimizer", "OptimizerWithMixedPrecision"
            ]:
                self.raiseOptimizeError("forward_recompute",
                                        self._optimizer.__class__.__name__)

            self._optimizer = \
                fluid.optimizer.RecomputeOptimizer(self._optimizer)
            self._optimizer._set_checkpoints(self._recompute_checkpoints)

        if self._use_amp:
            if self._optimizer.__class__.__name__ in [
                    "OptimizerWithMixedPrecision", "DGCMomentumOptimizer"
            ]:
                self.raiseOptimizeError("mixed_precision",
                                        self._optimizer.__class__.__name__)
            self._optimizer = fluid.contrib.mixed_precision.decorate(
                self._optimizer,
                init_loss_scaling=self._amp_loss_scaling,
                use_dynamic_loss_scaling=True)

        main_program = loss.block.program
        if startup_program is None:
            startup_program = fluid.default_startup_program()
        algorithm.startup_program = startup_program

        self._loss = loss

        self._check_collective_mode(main_program, self._optimizer,
                                    self._strategy)

        optimize_ops, param_grads = self._optimizer.minimize(
            loss,
            startup_program=startup_program,
            parameter_list=parameter_list,
            no_grad_set=no_grad_set)

        algorithm._origin_program = main_program.clone(for_test=False)
        algorithm._transpiled_program = main_program
        algorithm.main_program = self._try_to_compile(startup_program, main_program)

        return optimize_ops, param_grads
