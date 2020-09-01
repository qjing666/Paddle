# Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import print_function
import six
import collections
import warnings
import math

from functools import reduce
import paddle.fluid as fluid
import paddle.fluid.core as core
import paddle.fluid.framework as framework

from paddle.fluid.transpiler.details.program_utils import delete_ops
from paddle.fluid.incubate.fleet.parameter_server.ir.public import _get_optimize_ops
from paddle.fluid.incubate.fleet.parameter_server.ir.public import _get_lr_ops
from paddle.fluid.incubate.fleet.parameter_server.ir.public import get_sparse_tablenames
from paddle.fluid.incubate.fleet.parameter_server.mode import DistributedMode

OP_NAME_SCOPE = "op_namescope"
CLIP_OP_NAME_SCOPE = "@CLIP"
STEP_COUNTER = "@PS_STEP_COUNTER@"

OP_ROLE_VAR_ATTR_NAME = core.op_proto_and_checker_maker.kOpRoleVarAttrName()
RPC_OP_ROLE_ATTR_NAME = core.op_proto_and_checker_maker.kOpRoleAttrName()
RPC_OP_ROLE_ATTR_VALUE = core.op_proto_and_checker_maker.OpRole.RPC
LR_SCHED_OP_ROLE_ATTR_VALUE = core.op_proto_and_checker_maker.OpRole.LRSched
OPT_OP_ROLE_ATTR_VALUE = core.op_proto_and_checker_maker.OpRole.Optimize
op_role_attr_name = core.op_proto_and_checker_maker.kOpRoleAttrName()

DEVICE_LIST = ["cpu", "gpu", "xpu"]
COMMUNICATE_OPS_TYPE = ["send", "recv", "fetch_barrier", "send_barrier"]
DEFAULT_DEVICE = 'cpu'


def delete_optimizer_pass(program, config):
    def _delete_optimizer_op_and_vars(_program, optimize_ops):
        optimize_vars = []
        optimize_op_role_vars = []
        optimize_need_delete_vars = []

        for op in optimize_ops:
            optimize_vars.extend(op.input_arg_names)
            optimize_op_role_vars.extend(op.attr("op_role_var"))

        optimize_vars = list(set(optimize_vars))
        optimize_op_role_vars = list(set(optimize_op_role_vars))

        for var in optimize_vars:
            if var not in optimize_op_role_vars:
                optimize_need_delete_vars.append(var)
        need_delete_optimize_vars = list(set(optimize_need_delete_vars))

        delete_ops(_program.global_block(), optimize_ops)
        for var in need_delete_optimize_vars:
            if _program.global_block().has_var(var):
                _program.global_block()._remove_var(var)

    optimizer_ops = _get_optimize_ops(program)
    lr_ops = _get_lr_ops(program)
    optimizer_ops.extend(lr_ops)
    _delete_optimizer_op_and_vars(program, optimizer_ops)

    return program


def distributed_ops_pass(program, config):
    trainer_id = config.get_role_id()

    def _get_pull_sparse_ops(_program):
        pull_sparse_ops = {}
        op_types = {"lookup_table": "W"}
        for op in _program.global_block().ops:
            if op.type in op_types.keys() \
                    and op.attr('remote_prefetch') is True:
                param_name = op.input(op_types[op.type])[0]
                ops = pull_sparse_ops.get(param_name, [])
                ops.append(op)
                pull_sparse_ops[param_name] = ops
        return pull_sparse_ops

    def _pull_sparse_fuse(_program, pull_sparse_ops):
        for param, ops in pull_sparse_ops.items():
            all_ops = program.global_block().ops
            op_idxs = [all_ops.index(op) for op in ops]
            inputs = [
                program.global_block().vars[op.input("Ids")[0]] for op in ops
            ]
            w = program.global_block().vars[ops[0].input("W")[0]]
            padding_idx = ops[0].attr("padding_idx")
            is_distributed = ops[0].attr("is_distributed")

            outputs = [
                program.global_block().vars[op.output("Out")[0]] for op in ops
            ]

            for idx in op_idxs[::-1]:
                program.global_block()._remove_op(idx)

            inputs_idxs = [-1] * len(inputs)
            outputs_idxs = [-1] * len(outputs)

            for idx, op in enumerate(program.global_block().ops):
                for i in range(0, len(op.output_names)):
                    outs = op.output(op.output_names[i])
                    for in_id, in_var in enumerate(inputs):
                        if in_var.name in outs:
                            inputs_idxs[in_id] = idx
                for i in range(0, len(op.input_names)):
                    ins = op.input(op.input_names[i])
                    for out_id, out_var in enumerate(outputs):
                        if out_var.name in ins:
                            outputs_idxs[out_id] = idx

            tables = config.get_var_distributed(w.name, True)

            pserver_endpoints = config.get_ps_endpoints()

            tablenames, eps, sections, = [], [], []
            for table in tables:
                tablenames.append(table[0])
                eps.append(table[1])
                sections.append(table[2])

            if min(outputs_idxs) - max(inputs_idxs) >= 1:
                distributed_idx = max(inputs_idxs) + 1

                program.global_block()._insert_op(
                    index=distributed_idx,
                    type="distributed_lookup_table",
                    inputs={"Ids": inputs,
                            'W': w},
                    outputs={"Outputs": outputs},
                    attrs={
                        "table_names": tablenames,
                        "endpoints": eps,
                        "is_distributed": is_distributed,
                        "pserver_num": len(pserver_endpoints),
                        "padding_idx": padding_idx,
                        "trainer_id": trainer_id
                    })
            else:
                raise ValueError(
                    "something wrong with Fleet, submit a issue is recommended")

    pull_sparse_ops = _get_pull_sparse_ops(program)
    _pull_sparse_fuse(program, pull_sparse_ops)
    return program


def append_send_ops_pass(program, config):
    mode = config.get_distributed_mode()
    trainer_id = config.get_role_id()
    pserver_endpoints = config.get_ps_endpoints()

    def _append_send_op(union_vars, queue):

        if queue == STEP_COUNTER:
            send_input_vars = []
        else:
            send_input_vars = [
                program.global_block().vars[union_var]
                for union_var in union_vars
            ]

        dummy_output = []
        if mode in [DistributedMode.SYNC, DistributedMode.HALF_ASYNC]:
            dummy_output = program.global_block().create_var(
                name=framework.generate_control_dev_var_name())

        program.global_block().append_op(
            type="send",
            inputs={"X": send_input_vars},
            outputs={"Out": dummy_output},
            attrs={
                "send_varnames": [queue],
                "merge_add": True,
                "use_send_handler": False,
                "endpoints": pserver_endpoints,
                RPC_OP_ROLE_ATTR_NAME: RPC_OP_ROLE_ATTR_VALUE
            })

        return dummy_output

    def _append_barrier_op(dummys):
        program.global_block().append_op(
            type="send_barrier",
            inputs={"X": dummys},
            outputs={"Out": []},
            attrs={
                "endpoints": pserver_endpoints,
                "trainer_id": trainer_id,
                "half_async": True,
                RPC_OP_ROLE_ATTR_NAME: RPC_OP_ROLE_ATTR_VALUE
            })

    dummys = []

    sends = config.get_trainer_send_context()

    for merged_name, send in sends.items():
        dummys.append(_append_send_op(send.origin_varnames(), merged_name))

    if mode in [DistributedMode.SYNC, DistributedMode.HALF_ASYNC]:
        _append_barrier_op(dummys)

    return program


def init_from_server_pass(program, config):
    fetch_barrier_out = program.global_block().create_var(
        name=framework.generate_control_dev_var_name())

    recv_ctx = config.get_communicator_recv_context(recv_type=1)
    recv_varnames = []

    for name, ctxs in recv_ctx.items():
        recv_varnames.extend(ctxs.origin_varnames())

    program.global_block().append_op(
        type="recv",
        inputs={"X": []},
        outputs={"Out": []},
        attrs={
            "recv_varnames": recv_varnames,
            "trainer_id": config.get_role_id(),
            RPC_OP_ROLE_ATTR_NAME: RPC_OP_ROLE_ATTR_VALUE
        })

    program.global_block().append_op(
        type="fetch_barrier",
        inputs={},
        outputs={"Out": fetch_barrier_out},
        attrs={
            "endpoints": config.get_ps_endpoints(),
            "trainer_id": config.get_role_id(),
            RPC_OP_ROLE_ATTR_NAME: RPC_OP_ROLE_ATTR_VALUE
        })
    return program


def fake_init_ops_pass(program, config):
    origin_program = config.get_origin_main_program()

    def _get_sparse_table_names():
        dist_varnames = get_sparse_tablenames(origin_program, True)
        sparse_varnames = get_sparse_tablenames(origin_program, False)
        return list(set(dist_varnames + sparse_varnames))

    def _fake_init_sparsetable(sparse_table_names):
        # delete table init op
        for table_name in sparse_table_names:
            table_var = program.global_block().vars[table_name]
            table_param_init_op = []
            for op in program.global_block().ops:
                if table_name in op.output_arg_names:
                    table_param_init_op.append(op)
            init_op_num = len(table_param_init_op)
            if init_op_num != 1:
                raise ValueError("table init op num should be 1, now is " + str(
                    init_op_num))
            table_init_op = table_param_init_op[0]
            program.global_block().append_op(
                type="fake_init",
                inputs={},
                outputs={"Out": table_var},
                attrs={"shape": table_init_op.attr('shape')})
            delete_ops(program.global_block(), table_param_init_op)

    sparse_tables = _get_sparse_table_names()
    _fake_init_sparsetable(sparse_tables)

    return program


def delet_extra_optimizes_pass(program, config):
    optimize_vars = []
    optimize_op_role_vars = []
    optimize_need_delete_vars = []

    origin_program = config.get_origin_main_program()
    for op in _get_optimize_ops(origin_program):
        optimize_vars.extend(op.input_arg_names)
        optimize_op_role_vars.extend(op.attr("op_role_var"))

    optimize_vars = list(set(optimize_vars))
    optimize_op_role_vars = list(set(optimize_op_role_vars))

    for var in optimize_vars:
        if var not in optimize_op_role_vars:
            optimize_need_delete_vars.append(var)
    need_delete_optimize_vars = list(set(optimize_need_delete_vars))

    init_ops = []
    for var in need_delete_optimize_vars:
        param_init_op = []
        for op in program.global_block().ops:
            if var in op.output_arg_names:
                param_init_op.append(op)
        init_ops.extend(param_init_op)
    delete_ops(program.global_block(), init_ops)

    for var in need_delete_optimize_vars:
        if program.global_block().has_var(var):
            program.global_block()._remove_var(var)

    return program


def find_heter_ops(program, default_device="cpu"):
    if default_device not in DEVICE_LIST:
        raise ValueError("Given device {} is not in device list {}".format(
            default_device, DEVICE_LIST))

    def _is_heter_op(op, current_heter_device, default_device="cpu"):
        heter_devices = list(DEVICE_LIST)
        heter_devices.remove(default_device)
        op_device = op.attr("op_device")
        op_type = op.type
        if op_device in heter_devices:
            return True
        elif op_type in COMMUNICATE_OPS_TYPE and current_heter_device != default_device:
            # for distributed communciate ops: send & recv & barrier etc.
            # Todo: need update this method
            op._set_attr('op_device', current_heter_device)
            return True
        elif op_device == None or op_device == default_device:
            op._set_attr('op_device', default_device)
            return False
        return False

    def _is_same_device(op, pre_device, default_device="cpu"):
        op_device = op.attr("op_device")
        if op_device == pre_device:
            return True
        if pre_device == default_device:
            return True
        return False

    def _append_heter_op(op, current_heter_block_ops, heter_ops):
        op_device = op.attr("op_device")
        if op_device not in heter_ops:
            heter_ops[op_device] = {}
        current_heter_block_ops.append(op)

    origin_porgram = program.clone()
    block = program.global_block()

    program_block_ops = []
    default_ops = {default_device: {}}
    heter_ops = {}
    block_index = 0
    # heter_ops: {"gpu": {1:[op1, op2, ...], 2:[op1, op2, ...] }; "xpu": {3:[op1, op2, ...], 4:[op1, op2, ...] }}

    current_heter_block_ops = []
    current_default_block_ops = []
    current_heter_device = default_device
    is_heter = False
    for op in block.ops:
        if _is_heter_op(op, current_heter_device, default_device):
            # for gpu/xpu-op
            is_heter = True

            # for cpu-op block append
            if len(current_default_block_ops) > 1:
                default_ops[default_device][
                    block_index] = current_default_block_ops
                program_block_ops.append(current_default_block_ops)
                current_default_block_ops = []
                block_index += 1

            if _is_same_device(op, current_heter_device, default_device):
                # for gpu-op, gpu-op -> gpu-op,...
                current_heter_device = op.attr("op_device")
                _append_heter_op(op, current_heter_block_ops, heter_ops)
            else:
                # for gpu-op -> xpu-op, ...
                op_device = current_heter_block_ops[0].attr("op_device")
                heter_ops[op_device][block_index] = current_heter_block_ops
                program_block_ops.append(current_heter_block_ops)
                block_index += 1
                current_heter_block_ops = []
                current_heter_device = op.attr("op_device")
                _append_heter_op(op, current_heter_block_ops, heter_ops)

        elif is_heter:
            # for gpu/xpu-op -> cpu-op
            op_device = current_heter_block_ops[0].attr("op_device")
            heter_ops[op_device][block_index] = current_heter_block_ops
            program_block_ops.append(current_heter_block_ops)
            block_index += 1
            current_heter_block_ops = []
            current_heter_device = default_device
            is_heter = False
            current_default_block_ops.append(op)
        else:
            # for cpu-op
            current_default_block_ops.append(op)

    if current_default_block_ops != []:
        default_ops[default_device][block_index] = current_default_block_ops
        program_block_ops.append(current_default_block_ops)

    if current_heter_block_ops != []:
        op_device = current_heter_block_ops[0].attr("op_device")
        heter_ops[op_device][block_index] = current_heter_block_ops
        program_block_ops.append(current_heter_block_ops)

    if len(heter_ops) == 0:
        warnings.warn(
            "No heterogeneous OP was found in your program , "
            " please using fluid.device_guard() to run OPs on different device.")

    total_heter_ops = 0
    heter_blocks = 0
    for device in heter_ops.keys():
        heter_block_dict = heter_ops[device]
        heter_blocks += len(heter_block_dict)
        for _, heter_block in heter_block_dict.items():
            total_heter_ops += len(heter_block)
    print(
        "There are {} OPs in your main_program, and contains {} heter-OPs which is made up of {} heter-blocks.".
        format(len(block.ops), total_heter_ops, heter_blocks))
    return origin_porgram, heter_ops, default_ops, program_block_ops


def create_heter_program(program, config, heter_program, heter_ops,
                         block_var_detail, current_device):
    # add heter op
    optimizer_block = []
    grad_to_block_id = []
    send_grad_var_list = []

    pre_block_idx = heter_program.num_blocks - 1
    for index, heter_block_ops in heter_ops[current_device].items():
        heter_block = heter_program._create_block(pre_block_idx)
        optimizer_block.append(heter_block)
        for _, op in enumerate(heter_block_ops):
            block_append_op(heter_program, program, heter_block, op)

            # add relate variables
            inputs = _get_input_map_from_op(program.global_block().vars, op)
            add_vars_by_op_map(inputs, heter_program)

            outputs = _get_output_map_from_op(program.global_block().vars, op)
            add_vars_by_op_map(outputs, heter_program)

        entrance_vars = block_var_detail[index]["entrance"]
        add_vars_by_var_list(entrance_vars, program, heter_program)
        exit_vars = block_var_detail[index]["exit"]
        add_vars_by_var_list(exit_vars, program, heter_program)

        comm_info = get_communicate_var_info(program, index, entrance_vars,
                                             exit_vars)

        grad_to_block_id.append(comm_info["block_input_var_name"] + ":" + str(
            heter_block.idx))

        # create slice op
        first_op_index = 0

        get_type_var_name = comm_info["input_var_reshape_name"][0].split(
            ".input_reshape@Heter")[0]
        get_type_var = heter_program.global_block().vars[get_type_var_name]

        insert_recv_slice_op(
            heter_program, heter_block, first_op_index,
            comm_info["block_input_var_name"],
            (-1, sum(comm_info["input_var_reshape_dim"])), get_type_var.dtype,
            get_type_var.type, comm_info["input_var_reshape_name"], [
                (-1, comm_info["input_var_reshape_dim"][i])
                for i in range(len(comm_info["input_var_reshape_dim"]))
            ])
        first_op_index += len(comm_info["input_var_reshape_dim"])
        # create reshape op
        for i in range(len(comm_info["input_var_reshape_name"])):
            var_name = entrance_vars[i]
            insert_reshape_op(
                heter_program,
                heter_block,
                first_op_index,
                comm_info["input_var_reshape_name"][i],
                var_name, )
            first_op_index += 1

        first_op_index = len(heter_block.ops)

        # create send reshape op
        for i in range(len(exit_vars)):
            insert_reshape_op(heter_program, heter_block, first_op_index,
                              exit_vars[i],
                              comm_info["output_var_reshape_name"][i],
                              [-1, comm_info["output_var_reshape_dim"][i]])
            first_op_index += 1

        # create send concat op
        insert_send_concat_op(heter_program, heter_block, first_op_index,
                              comm_info["output_var_reshape_name"],
                              comm_info["block_output_var_name"],
                              [-1, sum(comm_info["output_var_reshape_dim"])])
        check_op_device(heter_block, current_device)
        send_grad_var_list = send_grad_var_list + add_heter_send_op(
            program, heter_program, heter_block, block_var_detail[index])

    # add step conter
    send_input_vars = []
    dummy_output = []
    trainer_id = config.get_role_id()
    pserver_endpoints = config.get_ps_endpoints()
    optimizer_block[-1].append_op(
        type="send",
        inputs={"X": send_input_vars},
        outputs={"Out": dummy_output},
        attrs={
            "send_varnames": [STEP_COUNTER],
            "merge_add": True,
            "use_send_handler": False,
            "endpoints": pserver_endpoints
        })

    # add info in listen&serv
    attrs = {
        "grad_to_block_id": grad_to_block_id,
        "sparse_grad_to_param": None,
        "lr_decay_block_id": None,
        "dense_optimize_blocks": None,
        "sparse_optimize_blocks": None,
        "optimize_blocks": optimizer_block,

        # runtime attribute
        "endpoint": config.get_heter_worker_endpoint(),
        "pserver_id": config.get_role_id(),
        "Fanin": config.get_trainers(),
        "distributed_mode": config.get_distributed_mode(),
        "rpc_get_thread_num": 12,
        "rpc_send_thread_num": 12,
        "rpc_prefetch_thread_num": 12
    }

    # append the listen_and_serv op
    heter_program.global_block().append_op(
        type="listen_and_serv", inputs={'X': []}, outputs={}, attrs=attrs)

    check_heter_compile_time_strategy(program, config, send_grad_var_list)


def check_heter_compile_time_strategy(program, config, send_grad_var_list):
    origin_grad_var_list = []
    for _, var_grad in config.merged_variables_pairs:
        origin_grad_var_list.append(var_grad.merged_var.name)

    origin_grad_var_list = list(set(origin_grad_var_list))
    send_grad_var_list = list(set(send_grad_var_list))
    useless_grad_var_list = list(
        set(origin_grad_var_list) - set(send_grad_var_list))

    for useless_grad_var in useless_grad_var_list:
        config.remove_var_pair_by_grad(useless_grad_var)


def create_trainer_program(program, config, heter_ops, block_var_detail):
    for device in heter_ops.keys():
        for heter_block_index in sorted(heter_ops[device]):
            replace_ops_by_communicate_op(program, config, heter_block_index,
                                          heter_ops[device][heter_block_index],
                                          block_var_detail)
            remove_trainer_send_op(program, config, heter_block_index,
                                   block_var_detail)
    deleter_trainer_useless_var(program)
    check_op_device(program.global_block(), DEFAULT_DEVICE)


def replace_ops_by_communicate_op(program, config, heter_block_index, ops_list,
                                  block_var_detail):
    all_op = program.global_block().ops
    start_op = ops_list[0]
    first_op_idx = -1
    for op in all_op:
        if is_same_op(op, start_op):
            first_op_idx = all_op.index(op)
            break
    assert first_op_idx != -1
    delete_same_ops(program.global_block(), ops_list)

    mode = config.get_distributed_mode()
    heter_worker_endpoint = config.get_heter_worker_endpoint()
    entrance_var = block_var_detail[heter_block_index]["entrance"]
    exit_var = block_var_detail[heter_block_index]["exit"]

    default_device_comm_info = get_communicate_var_info(
        program, heter_block_index - 1,
        block_var_detail[heter_block_index - 1]["entrance"],
        block_var_detail[heter_block_index - 1]["exit"])
    comm_info = get_communicate_var_info(program, heter_block_index,
                                         entrance_var, exit_var)

    # create reshape op
    for i in range(len(entrance_var)):
        insert_reshape_op(
            program,
            program.global_block(), first_op_idx, entrance_var[i],
            default_device_comm_info["output_var_reshape_name"][i],
            [-1, default_device_comm_info["output_var_reshape_dim"][i]])
        first_op_idx += 1

    # create concat op
    insert_send_concat_op(
        program,
        program.global_block(), first_op_idx,
        default_device_comm_info["output_var_reshape_name"],
        default_device_comm_info["block_output_var_name"],
        [-1, sum(default_device_comm_info["output_var_reshape_dim"])])
    first_op_idx += 1

    # create send op
    send_input_vars = [
        program.global_block().vars[default_device_comm_info[
            "block_output_var_name"]]
    ]

    get_type_var_name = comm_info["output_var_reshape_name"][0].split(
        ".output_reshape@Heter")[0]
    get_type_var = program.global_block().vars[get_type_var_name]

    program.global_block().create_var(
        name=comm_info["block_output_var_name"],
        shape=(-1, sum(comm_info["output_var_reshape_dim"])),
        dtype=get_type_var.dtype,
        type=get_type_var.type)

    recv_vars = [
        program.global_block().vars[comm_info["block_output_var_name"]]
    ]

    program.global_block()._insert_op(
        index=first_op_idx,
        type="send_and_recv",
        inputs={"X": send_input_vars},
        outputs={"Out": recv_vars},
        attrs={
            "send_var_name": default_device_comm_info["block_output_var_name"],
            "recv_var_name": comm_info["block_output_var_name"],
            "endpoint": heter_worker_endpoint,
            "trainer_id": config.get_role_id(),
            RPC_OP_ROLE_ATTR_NAME: RPC_OP_ROLE_ATTR_VALUE
        })
    first_op_idx += 1

    # recv
    # create slice op
    insert_recv_slice_op(
        program,
        program.global_block(), first_op_idx,
        comm_info["block_output_var_name"],
        (-1, sum(comm_info["output_var_reshape_dim"])), get_type_var.dtype,
        get_type_var.type, comm_info["output_var_reshape_name"], [
            (-1, comm_info["output_var_reshape_dim"][i])
            for i in range(len(comm_info["output_var_reshape_dim"]))
        ])

    first_op_idx += len(comm_info["output_var_reshape_dim"])

    # create reshape op
    for i in range(len(comm_info["output_var_reshape_name"])):
        var_name = comm_info["output_var_reshape_name"][i].split(
            ".output_reshape@Heter")[0]
        insert_reshape_op(
            program,
            program.global_block(),
            first_op_idx,
            comm_info["output_var_reshape_name"][i],
            var_name, )
        first_op_idx += 1


def remove_trainer_send_op(program, config, heter_block_index,
                           block_var_detaile):
    # if trainer do FF->BP->SEND, it has follow vars: var, var@GRAD
    # if trainer only do SEND, it has one var: var@GRAD
    # Delete Send op ,if trainer doesn't has pair var (var<->var@GRAD)
    persistables = block_var_detaile[heter_block_index]["persistables"]
    need_remove_send_op = []
    need_remove_grad_var = []
    for op in find_send_op(program):
        input_list, _ = find_op_input_output(program,
                                             program.global_block(), op)
        for var_name in input_list:
            origin_var_name = var_name.split("@GRAD")[0]
            if origin_var_name in persistables:
                need_remove_send_op.append(op)
                need_remove_grad_var.append(var_name)
    need_remove_send_op = list(set(need_remove_send_op))
    delete_ops(program.global_block(), need_remove_send_op)
    for grad_var_name in need_remove_grad_var:
        config.remove_var_pair_by_grad(grad_var_name)


def add_heter_send_op(program, heter_program, block, block_var_detail):
    def _get_send_op_dict():
        send_op_dict = {}
        send_op_list = find_send_op(program)
        for op in send_op_list:
            input_list, _ = find_op_input_output(program,
                                                 program.global_block(), op)
            for var in input_list:
                send_op_dict[var] = op
        return send_op_dict

    send_grad_var_list = []
    send_op_dict = _get_send_op_dict()
    for persistable_var in block_var_detail["persistables"]:
        # check var_name ==  var@GRAD
        if "@GRAD" not in persistable_var:
            continue
        if "GRAD" != persistable_var.split("@")[-1]:
            continue
        if persistable_var not in send_op_dict:
            continue
        block_append_op(program, heter_program, block,
                        send_op_dict[persistable_var])
        send_grad_var_list.append(persistable_var)
    return send_grad_var_list


def find_send_op(program):
    send_op_list = []
    for op in program.global_block().ops:
        if op.type == "send":
            send_op_list.append(op)
    return send_op_list


def get_communicate_var_info(program, block_index, entrance_var_list,
                             exit_var_list):
    input_var_reshape_dim = []
    input_var_reshape_name = []
    block_input_var_name = "joint_{}_{}@Heter".format(block_index - 1,
                                                      block_index)
    output_var_reshape_dim = []
    output_var_reshape_name = []
    block_output_var_name = "joint_{}_{}@Heter".format(block_index,
                                                       block_index + 1)
    entrance_var_list.sort()
    exit_var_list.sort()
    # input
    # Heter_SERVER_BLOCK_index@JOINT_VAR -> slice -> var@Heter_SERVER_BLOCK@INPUT_RESHAPE_VAR -> reshape -> var
    for name in entrance_var_list:
        var = program.global_block().vars[name]
        shape = var.shape
        if len(shape) < 2 or shape[0] != -1:
            raise ValueError(
                "Variable {} not support heter training. its shape is {}".
                format(name, shape))
        recv_var_dim = -1 * reduce(lambda x, y: x * y, shape)
        input_var_reshape_dim.append(recv_var_dim)
        input_var_reshape_name.append("{}.input_reshape@Heter".format(name))

    # output
    # var -> reshape -> var@Heter_SERVER_BLOCK@INPUT_RESHAPE_VAR -> concat -> Heter_SERVER_BLOCK_index@JOINT_VAR
    for var_name in exit_var_list:
        var = program.global_block().vars[var_name]
        shape = var.shape
        if len(shape) < 2 or shape[0] != -1:
            raise ValueError(
                "Variable {} not support heter training. its shape is {}".
                format(var_name, shape))
        send_reshape_dim = -1 * reduce(lambda x, y: x * y, shape)
        output_var_reshape_dim.append(send_reshape_dim)
        output_var_reshape_name.append("{}.output_reshape@Heter".format(
            var_name))

    info = {
        "input_var_reshape_dim": input_var_reshape_dim,
        "input_var_reshape_name": input_var_reshape_name,
        "block_input_var_name": block_input_var_name,
        "output_var_reshape_dim": output_var_reshape_dim,
        "output_var_reshape_name": output_var_reshape_name,
        "block_output_var_name": block_output_var_name
    }

    return info


def find_block_joints(program, program_block_ops_list, heter_ops):
    block_var_detail = find_entrance_exit_private(program,
                                                  program_block_ops_list)
    block_var_detail = entrance_exit_check(program, program_block_ops_list,
                                           block_var_detail, heter_ops)
    block_var_detail = delete_block_useless_exit(
        program, program_block_ops_list, block_var_detail)
    return block_var_detail


def find_entrance_exit_private(program, program_block_ops_list):
    block_var_detail = []
    persistables = []
    for index, block_op_list in enumerate(program_block_ops_list):
        block_input, block_output = find_ops_list_input_output(program,
                                                               block_op_list)
        persistables = screen_persistables(
            program, block_input) + screen_persistables(program, block_output)
        # find entrance & exit
        block_private_vars = list(set(block_input) & set(block_output))
        block_entrance = list(set(block_input) - set(block_private_vars))
        block_exit = list(set(block_output) - set(block_private_vars))
        detail = {
            "entrance": block_entrance,
            "exit": block_exit,
            "private": block_private_vars,
            "persistables": persistables
        }
        block_var_detail.append(detail)
    return block_var_detail


def entrance_exit_check(program, program_block_ops_list, block_var_detail,
                        heter_ops):
    for index in range(len(block_var_detail) - 1, -1, -1):
        if index - 1 < 0:
            break
        previous_block_exit = block_var_detail[index - 1]["exit"]
        previous_block_exit.sort()
        current_block_entrance = block_var_detail[index]["entrance"]
        current_block_entrance.sort()
        if previous_block_exit == current_block_entrance:
            continue
        exist_vars = list(
            set(previous_block_exit) & set(current_block_entrance))
        need_add_vars = list(set(current_block_entrance) - set(exist_vars))
        need_add_vars = find_need_var_from_previous_block(
            need_add_vars, block_var_detail, index, heter_ops)

        previous_block_private = block_var_detail[index - 1]["private"]
        previous_block_entrance = block_var_detail[index - 1]["entrance"]
        for var in need_add_vars:
            if var not in previous_block_private and var not in previous_block_entrance:
                previous_block_entrance.append(var)
            previous_block_exit.append(var)
    return block_var_detail


def find_need_var_from_previous_block(need_add_vars, block_var_detail,
                                      current_index, heter_ops):
    # create index_device_map
    index_device_map = {}
    for index in range(len(block_var_detail)):
        index_device_map[index] = DEFAULT_DEVICE
    for device in heter_ops:
        for index in heter_ops[device].keys():
            index_device_map[index] = device

    pre_index = current_index - 1
    need_ignore_var = []

    # if need_add_var in current device, no need communicate
    for var in need_add_vars:
        while (pre_index >= 0):
            previous_block_private = block_var_detail[pre_index]["private"]
            previous_block_exit = block_var_detail[pre_index]["exit"]
            previous_block_entrance = block_var_detail[pre_index]["entrance"]
            total_var = previous_block_private + previous_block_exit + previous_block_entrance
            if var in total_var:
                if index_device_map[current_index] == index_device_map[
                        pre_index] and index_device_map[
                            current_index] == DEFAULT_DEVICE:
                    need_ignore_var.append(var)
                    break
            pre_index -= 1

    need_add_vars = list(set(need_add_vars).difference(set(need_ignore_var)))
    return need_add_vars


def delete_block_useless_exit(program, program_block_ops_list,
                              block_var_detail):
    for index in range(len(block_var_detail)):
        if index == len(block_var_detail) - 1:
            break
        current_block_exit = block_var_detail[index]["exit"]
        next_block_entrance = block_var_detail[index + 1]["entrance"]
        need_delete_var = []
        for var in current_block_exit:
            if var not in next_block_entrance:
                need_delete_var.append(var)

        for var in need_delete_var:
            current_block_exit.remove(var)

    return block_var_detail


def check_op_device(block, device):
    for op in block.ops:
        op._set_attr('op_device', device)


def screen_persistables(program, var_list):
    need_remove = []
    for var_name in var_list:
        if "@GRAD" in var_name:
            origin_var_name = var_name.split("@GRAD")[0]
            var = program.global_block().vars[origin_var_name]
        else:
            var = program.global_block().vars[var_name]

        if fluid.io.is_persistable(var):
            need_remove.append(var_name)

    for var_name in need_remove:
        var_list.remove(var_name)
    return need_remove


def insert_reshape_op(program,
                      block,
                      index,
                      var_name,
                      new_var_name,
                      new_var_shape=None):
    input_var = program.global_block().vars[var_name]

    if new_var_name not in program.global_block().vars:
        out = program.global_block().create_var(
            name=new_var_name,
            shape=new_var_shape,
            dtype=input_var.dtype,
            type=input_var.type)
    else:
        out = program.global_block().vars[new_var_name]
        new_var_shape = out.shape

    x_shape = program.global_block().create_var(
        name="{}.xshape@Heter".format(var_name), dtype=input_var.dtype)
    block._insert_op(
        index=index,
        type="reshape2",
        inputs={"X": input_var},
        attrs={'shape': new_var_shape},
        outputs={"Out": out,
                 "XShape": x_shape})


def insert_send_concat_op(program, block, index, var_name_list, new_var_name,
                          new_var_shape):
    input_var_list = [
        program.global_block().vars[var_name] for var_name in var_name_list
    ]

    out = program.global_block().create_var(
        name=new_var_name,
        shape=new_var_shape,
        dtype=input_var_list[0].dtype,
        type=input_var_list[0].type)

    block._insert_op(
        index=index,
        type='concat',
        inputs={"X": input_var_list},
        outputs={'Out': [out]},
        attrs={'axis': -1,
               'use_stack': False})


def insert_recv_slice_op(program, block, index, var_name, var_shape, dtype,
                         type, new_var_name_list, new_var_shape_list):

    if var_name not in program.global_block().vars:
        input_var = program.global_block().create_var(
            name=var_name, shape=var_shape, dtype=dtype, type=type)
    else:
        input_var = program.global_block().vars[var_name]

    out_list = []
    for i in range(len(new_var_name_list)):
        if new_var_name_list[i] not in program.global_block().vars:
            out = program.global_block().create_var(
                name=new_var_name_list[i],
                shape=new_var_shape_list[i],
                dtype=input_var.dtype,
                type=input_var.type)
        else:
            out = program.global_block().vars[new_var_name_list[i]]
        out_list.append(out)

    start_index = 0
    end_index = 0
    for i in range(len(new_var_name_list)):
        starts = []
        ends = []
        attrs = {'axes': [1]}
        end_index += new_var_shape_list[i][1]
        starts.append(start_index)
        ends.append(end_index)
        attrs['starts'] = starts
        attrs['ends'] = ends

        block._insert_op(
            index=index,
            type='slice',
            inputs={'Input': input_var},
            attrs=attrs,
            outputs={'Out': out_list[i]})
        start_index = end_index
        index += 1


def deleter_trainer_useless_var(program):
    porgram_useful_var_list = []
    for op in program.global_block().ops:
        input_var_list, output_var_list = find_op_input_output(
            program, program.global_block(), op)
        op_var_list = list(set(input_var_list).union(set(output_var_list)))
        porgram_useful_var_list = list(
            set(porgram_useful_var_list).union(set(op_var_list)))

    program_useless_var_list = list(
        set(get_vars_name_in_block(program.global_block())).difference(
            set(porgram_useful_var_list)))
    for var in program_useless_var_list:
        program.global_block()._remove_var(var)
    return program_useless_var_list


def block_append_op(program, origin_program, block, op):
    inputs = _get_input_map_from_op(origin_program.global_block().vars, op)
    for key, varlist in six.iteritems(inputs):
        if not isinstance(varlist, list):
            varlist = [varlist]
        for var in varlist:
            if var.name not in program.global_block().vars:
                program.global_block()._clone_variable(var)

    outputs = _get_output_map_from_op(origin_program.global_block().vars, op)
    for key, varlist in six.iteritems(outputs):
        if not isinstance(varlist, list):
            varlist = [varlist]
        for var in varlist:
            if var.name not in program.global_block().vars:
                program.global_block()._clone_variable(var)

    if "_grad" not in op.type:
        # for forward op
        return block.append_op(
            type=op.type, inputs=inputs, outputs=outputs, attrs=op.all_attrs())
    else:
        # for grad op
        op_desc = op.desc
        op_role_attr_name = core.op_proto_and_checker_maker.kOpRoleAttrName()
        backward = core.op_proto_and_checker_maker.OpRole.Backward
        device_attr_name = core.op_proto_and_checker_maker.kOpDeviceAttrName()

        # append grad op
        new_op_desc = block.desc.append_op()
        new_op_desc.copy_from(op_desc)
        new_op_desc._set_attr(op_role_attr_name, backward)

        # set device gard
        if op.desc.has_attr(device_attr_name):
            op_device = op_desc.attr(device_attr_name)
            new_op_desc._set_attr(device_attr_name, op_device)
        block._sync_with_cpp()


def add_vars_by_op_map(var_map, program):
    for key, varlist in six.iteritems(var_map):
        if not isinstance(varlist, list):
            varlist = [varlist]
        for i in range(len(varlist)):
            var = varlist[i]
            if var.name not in program.global_block().vars:
                program.global_block()._clone_variable(var)


def add_vars_by_var_list(var_name_list, origin_program, program):
    for var_name in var_name_list:
        if var_name not in program.global_block().vars:
            var = origin_program.global_block().vars[var_name]
            program.global_block()._clone_variable(var)


def get_varlist_from_op_map(var_map):
    var_list = []
    for key, varlist in six.iteritems(var_map):
        if not isinstance(varlist, list):
            varlist = [varlist]
        for i in range(len(varlist)):
            var = varlist[i]
            var_list.append(var.name)
    return var_list


def find_ops_list_input_output(program, ops_list):
    input_var_list = []
    output_var_list = []
    for op in ops_list:
        inputs = _get_input_map_from_op(program.global_block().vars, op)
        input_var_list += get_varlist_from_op_map(inputs)
        outputs = _get_output_map_from_op(program.global_block().vars, op)
        output_var_list += get_varlist_from_op_map(outputs)

    input_var_list = list(set(input_var_list))
    output_var_list = list(set(output_var_list))
    return input_var_list, output_var_list


def find_op_input_output(program, block, op):
    input_var_list = []
    output_var_list = []
    inputs = _get_input_map_from_op(block.vars, op)
    input_var_list += get_varlist_from_op_map(inputs)
    outputs = _get_output_map_from_op(block.vars, op)
    output_var_list += get_varlist_from_op_map(outputs)
    input_var_list = list(set(input_var_list))
    output_var_list = list(set(output_var_list))
    return input_var_list, output_var_list


def get_vars_name_in_block(block):
    vars_list = block.vars.keys()
    vars_name_list = [var_name for var_name in vars_list]
    return vars_name_list


def is_same_op(op1, op2):
    if str(op1) != str(op2):
        return False
    return True


def _get_input_map_from_op(varmap, op):
    """Returns a dict from op input name to the vars in varmap."""
    iomap = collections.OrderedDict()
    for key in op.input_names:
        vars = []
        for varname in op.input(key):
            if varname == "@EMPTY@":
                continue
            if "lod_tensor_blocking_queue" in varname:
                continue
            vars.append(varmap[varname])
        if len(vars) == 1:
            iomap[key] = vars[0]
        else:
            iomap[key] = vars
    return iomap


def _get_output_map_from_op(varmap, op):
    """Returns a dict from op output name to the vars in varmap."""
    iomap = collections.OrderedDict()
    for key in op.output_names:
        vars = []
        for varname in op.output(key):
            if varname == "@EMPTY@":
                continue
            if "lod_tensor_blocking_queue" in varname:
                continue
            vars.append(varmap[varname])
        if len(vars) == 1:
            iomap[key] = vars[0]
        else:
            iomap[key] = vars
    return iomap


def delete_same_ops(block, ops):
    for op in ops:
        try:
            for origin_op in block.ops:
                if is_same_op(origin_op, op):
                    idx = list(block.ops).index(origin_op)
                    block._remove_op(idx)
                    break
        except Exception as e:
            print(e)
