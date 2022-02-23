# Owner(s): ["oncall: distributed"]
import itertools
import math
import sys
from copy import deepcopy

import torch
import torch.nn as nn
from torch import distributed as dist
from torch.distributed.fsdp import CPUOffload
from torch.distributed.fsdp import FlatParameter
from torch.distributed.fsdp import FullyShardedDataParallel as FSDP
from torch.testing._internal.common_distributed import skip_if_lt_x_gpu
from torch.testing._internal.common_fsdp import (
    FSDPInitMode,
    FSDPTest,
    NestedWrappedModule,
)
from torch.testing._internal.common_utils import (
    TEST_WITH_DEV_DBG_ASAN,
    run_tests,
    instantiate_parametrized_tests,
    parametrize,
)


if not dist.is_available():
    print("Distributed not available, skipping tests", file=sys.stderr)
    sys.exit(0)

if TEST_WITH_DEV_DBG_ASAN:
    print(
        "Skip dev-asan as torch + multiprocessing spawn have known issues",
        file=sys.stderr,
    )
    sys.exit(0)


def _run_test_summon_full_param_writeback(cls, writeback, cpu_offload, modify_outer):
    model = FSDP(
        nn.Sequential(FSDP(nn.Linear(5, 5, bias=False)), nn.Linear(5, 3, bias=False))
    ).cuda(cls.rank)

    # set the value
    outer_param = model.get_parameter("_fsdp_wrapped_module.flat_param")
    inner_param = model.get_parameter(
        "_fsdp_wrapped_module._fpw_module.0._fsdp_wrapped_module.flat_param"
    )
    p = outer_param if modify_outer else inner_param

    with torch.no_grad():
        # This sets the local shard value
        p[0] = cls.rank + 2

    with model._summon_full_params(writeback=writeback):
        with torch.no_grad():
            p.copy_(torch.zeros_like(p))

    if writeback or cls.world_size == 1:
        # When world_size = 1, FSDP does not shard and parameter is not set to
        # a local shard, so write is always reflected.
        cls.assertEqual(p.cpu()[0], 0)
    else:
        cls.assertEqual(p.cpu()[0], cls.rank + 2)


class TestSummonFullParamsNoShard(FSDPTest):
    @property
    def world_size(self):
        return 1  # does not shard

    @skip_if_lt_x_gpu(2)
    @parametrize("writeback", [True, False])
    @parametrize(
        "cpu_offload",
        [CPUOffload(offload_params=True), CPUOffload(offload_params=False)],
    )
    @parametrize("modify_outer", [True, False])
    def test_summon_full_param_writeback(self, writeback, cpu_offload, modify_outer):
        return _run_test_summon_full_param_writeback(
            self,
            writeback,
            cpu_offload,
            modify_outer,
        )


class TestSummonFullParams(FSDPTest):
    @property
    def world_size(self):
        return 2

    def get_model_param_count(self, m):
        return sum([p.numel() for p in m.parameters()])

    # padding ensures that all shards have the same size with the least amount of padding
    def get_expected_sharded_size(self, global_size):
        return int(math.ceil(global_size / self.world_size))

    @skip_if_lt_x_gpu(2)
    @parametrize("writeback", [True, False])
    @parametrize(
        "cpu_offload",
        [CPUOffload(offload_params=True), CPUOffload(offload_params=False)],
    )
    @parametrize("modify_outer", [True, False])
    def test_summon_full_param_writeback(self, writeback, cpu_offload, modify_outer):
        return _run_test_summon_full_param_writeback(
            self, writeback, cpu_offload, modify_outer
        )

    @skip_if_lt_x_gpu(2)
    def test_summon_full_param_shard_value(self):

        raw_model = nn.Linear(10, 11)
        raw_model_size = self.get_model_param_count(raw_model)
        expected_shard_size = self.get_expected_sharded_size(raw_model_size)

        model = FSDP(raw_model.cuda(self.rank))
        self.assertEqual(expected_shard_size, self.get_model_param_count(model))

        # we're assuming a single flatenned param
        self.assertEqual(1, len(list(model.parameters())))

        my_shard = torch.clone(next(model.parameters()))

        with model._summon_full_params():
            self.assertEqual(raw_model_size, self.get_model_param_count(model))
            parameters = list(model.parameters())
            all_shards = FlatParameter(parameters, requires_grad=False)
            my_slice = torch.chunk(all_shards, self.world_size)[self.rank]

            # shards are padded but the full_param tensor is not
            a, b = my_shard[0 : my_slice.numel()], my_slice
            self.assertTrue(
                torch.equal(my_shard[0 : my_slice.numel()].cpu(), my_slice.cpu())
            )

    @skip_if_lt_x_gpu(2)
    @parametrize("recurse", [True, False])
    @parametrize("summon_outer", [True, False])
    def test_summon_full_param_recursive(self, recurse, summon_outer):
        model = FSDP(
            nn.Sequential(
                FSDP(nn.Linear(5, 5, bias=False)), nn.Linear(5, 3, bias=False)
            )
        ).cuda(self.rank)

        global_inner_numel = self.get_model_param_count(nn.Linear(5, 5, bias=False))
        global_outer_numel = self.get_model_param_count(nn.Linear(5, 3, bias=False))

        shard_inner_numel = int(math.ceil(global_inner_numel / self.world_size))
        shard_outer_numel = int(math.ceil(global_outer_numel / self.world_size))

        outer_param = model.get_parameter("_fsdp_wrapped_module.flat_param")
        inner_param = model.get_parameter(
            "_fsdp_wrapped_module._fpw_module.0._fsdp_wrapped_module.flat_param"
        )
        self.assertEqual(shard_outer_numel, outer_param.numel())
        self.assertEqual(shard_inner_numel, inner_param.numel())

        model_to_summon = model if summon_outer else model[0]
        # outer is summoned if _summon_full_param is called on the outer FSDP module
        expected_outer_numel = global_outer_numel if summon_outer else shard_outer_numel

        # inner is summoned if _summon_full_param is called with recursion or on the inner FSDP module
        expected_inner_numel = (
            global_inner_numel if recurse or not summon_outer else shard_inner_numel
        )

        with model_to_summon._summon_full_params(recurse=recurse):
            self.assertEqual(expected_outer_numel, outer_param.numel())
            self.assertEqual(expected_inner_numel, inner_param.numel())

    @skip_if_lt_x_gpu(2)
    def test_cannot_summon_full_params_from_forward(self):
        class MyModule(nn.Module):
            def __init__(self):
                super().__init__()
                self.a = nn.Parameter(torch.zeros(5))

            def forward(self, fsdp_module):
                with fsdp_module._summon_full_params():
                    pass

        model = FSDP(MyModule()).cuda(self.rank)
        with self.assertRaisesRegex(
            ValueError, "current state is TrainingState_.FORWARD"
        ):
            model(model)

    @skip_if_lt_x_gpu(2)
    def test_cannot_summon_full_params_from_backward(self):
        model = FSDP(nn.Linear(2, 1)).cuda(self.rank)

        output = model(torch.ones(2).cuda(self.rank))

        def bad_backwards_hook(tensor):
            with model._summon_full_params():
                pass
            return None

        self.assertTrue(output.requires_grad)
        output.register_hook(bad_backwards_hook)

        with self.assertRaisesRegex(
            ValueError, "current state is TrainingState_.BACKWARD_PRE"
        ):
            output.backward()

    @skip_if_lt_x_gpu(2)
    def test_summon_full_params_respects_reshard_after_forward(self):
        model = FSDP(
            nn.Sequential(
                FSDP(nn.Linear(5, 5, bias=False)), nn.Linear(5, 3, bias=False)
            )
        ).cuda(self.rank)

        outer_param = model.get_parameter("_fsdp_wrapped_module.flat_param")
        inner_param = model.get_parameter(
            "_fsdp_wrapped_module._fpw_module.0._fsdp_wrapped_module.flat_param"
        )
        outer_full_param_size = outer_param.numel() * self.world_size

        # trigger lazy init
        model(torch.zeros(5).cuda(self.rank))

        # the root FSDP module keeps all params around
        self.assertEqual(
            outer_full_param_size, outer_param._full_param_padded.storage().size()
        )
        self.assertEqual(0, inner_param._full_param_padded.storage().size())

        # similarly _summon_full_params should have the same behavior
        with model._summon_full_params():
            pass
        self.assertEqual(
            outer_full_param_size, outer_param._full_param_padded.storage().size()
        )
        self.assertEqual(0, inner_param._full_param_padded.storage().size())

    @skip_if_lt_x_gpu(2)
    def test_summon_single_param(self):
        model = FSDP(nn.Linear(1, 1, bias=False)).cuda(self.rank)

        p = model.get_parameter("_fsdp_wrapped_module.flat_param")
        self.assertEqual(1, p.numel())

        with torch.no_grad():
            # This sets the local shard value
            p[0] = self.rank + 2

        with model._summon_full_params(writeback=True):
            self.assertEqual(1, p.numel())
            with torch.no_grad():
                p.copy_(torch.zeros_like(p))

        # most ranks hold no data and wrote to padding so only rank zero will observe the above write
        if self.rank == 0:
            self.assertEqual(0, p[0])
        else:
            self.assertEqual(self.rank + 2, p[0])

    @skip_if_lt_x_gpu(2)
    def test_reshard_outside_forward_backward_iteration(self):
        model = FSDP(
            nn.Sequential(
                FSDP(nn.Linear(5, 5, bias=False)), nn.Linear(5, 1, bias=False)
            )
        ).cuda(self.rank)

        outer_param = model.get_parameter("_fsdp_wrapped_module.flat_param")
        inner_param = model.get_parameter(
            "_fsdp_wrapped_module._fpw_module.0._fsdp_wrapped_module.flat_param"
        )
        outer_full_param_size = outer_param.numel() * self.world_size

        # First lets validate our assumption about resharding

        output = model(torch.zeros(5).cuda(self.rank))
        # the root FSDP module keeps all params around
        self.assertEqual(
            outer_full_param_size, outer_param._full_param_padded.storage().size()
        )
        self.assertEqual(0, inner_param._full_param_padded.storage().size())

        output.backward()
        # we reshard everything after backward() finishes
        self.assertEqual(0, outer_param._full_param_padded.storage().size())
        self.assertEqual(0, inner_param._full_param_padded.storage().size())

        # now lets repeat it with summon done in between

        output = model(torch.zeros(5).cuda(self.rank))
        with model._summon_full_params():
            pass
        self.assertEqual(
            outer_full_param_size, outer_param._full_param_padded.storage().size()
        )
        self.assertEqual(0, inner_param._full_param_padded.storage().size())

        output.backward()
        with model._summon_full_params():
            pass
        self.assertEqual(0, outer_param._full_param_padded.storage().size())
        self.assertEqual(0, inner_param._full_param_padded.storage().size())

    @skip_if_lt_x_gpu(2)
    def test_params_are_unflattenned(self):
        layer_shape = (10, 12)
        model = nn.Linear(*layer_shape, bias=False).cuda(self.rank)
        fsdp_model = FSDP(deepcopy(model)).cuda(self.rank)

        flattened_param = fsdp_model.get_parameter("_fsdp_wrapped_module.flat_param")
        self.assertEqual(layer_shape[0] * layer_shape[1] / 2, flattened_param.numel())

        with fsdp_model._summon_full_params():
            self.assertEqual(fsdp_model.weight.shape, model.weight.shape)

    @skip_if_lt_x_gpu(2)
    def test_params_count_and_value(self):
        fsdp_model = FSDP(
            NestedWrappedModule(
                group=dist.distributed_c10d._get_default_group(),
                wrap_fsdp=True,
                fsdp_init_mode=FSDPInitMode.CUDA_BEFORE,
            )
        )
        model = NestedWrappedModule(
            group=dist.distributed_c10d._get_default_group(),
            wrap_fsdp=False,
            fsdp_init_mode=FSDPInitMode.CUDA_BEFORE,
        )
        with fsdp_model._summon_full_params():
            for p1, p2 in itertools.zip_longest(
                fsdp_model.parameters(), model.module.parameters()
            ):
                self.assertEqual(p1, p2)


instantiate_parametrized_tests(TestSummonFullParams)
instantiate_parametrized_tests(TestSummonFullParamsNoShard)


if __name__ == "__main__":
    run_tests()
