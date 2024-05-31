#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

# pyre-ignore-all-errors[56]

import itertools
import logging
from math import log2
from typing import List, Optional, Tuple, Type

import torch  # usort:skip
import functools

import fbgemm_gpu.split_embedding_codegen_lookup_invokers as invokers
from fbgemm_gpu.split_embedding_configs import EmbOptimType as OptimType, SparseType

from fbgemm_gpu.split_table_batched_embeddings_ops_common import (
    CacheAlgorithm,
    DEFAULT_SCALE_BIAS_SIZE_IN_BYTES,
    EmbeddingLocation,
    PoolingMode,
    SplitState,
)
from fbgemm_gpu.split_table_batched_embeddings_ops_inference import (
    align_to_cacheline,
    rounded_row_size_in_bytes,
    unpadded_row_size_in_bytes,
)
from fbgemm_gpu.split_table_batched_embeddings_ops_training import (
    apply_split_helper,
    CounterBasedRegularizationDefinition,
    CowClipDefinition,
    WeightDecayMode,
)

from torch import nn, Tensor  # usort:skip
from torch.autograd.profiler import record_function

try:
    if torch.version.hip:
        torch.ops.load_library(
            "//deeplearning/fbgemm/fbgemm_gpu:ssd_split_table_batched_embeddings_hip"
        )
    else:
        torch.ops.load_library(
            "//deeplearning/fbgemm/fbgemm_gpu:ssd_split_table_batched_embeddings"
        )
except OSError:
    # Keep for BC: will be deprecated soon.
    torch.ops.load_library(
        "//deeplearning/fbgemm/fbgemm_gpu/fb:ssd_split_table_batched_embeddings"
    )

ASSOC = 32


class SSDTableBatchedEmbeddingBags(nn.Module):
    D_offsets: Tensor
    lxu_cache_weights: Tensor
    lru_state: Tensor
    lxu_cache_weights: Tensor
    lxu_cache_state: Tensor
    momentum1_dev: Tensor
    momentum1_uvm: Tensor
    momentum1_host: Tensor
    momentum1_placements: Tensor
    momentum1_offsets: Tensor
    weights_dev: Tensor
    weights_uvm: Tensor
    weights_host: Tensor
    weights_placements: Tensor
    weights_offsets: Tensor

    def __init__(
        self,
        embedding_specs: List[Tuple[int, int]],  # tuple of (rows, dims)
        feature_table_map: Optional[List[int]],  # [T]
        cache_sets: int,
        ssd_storage_directory: str,
        ssd_shards: int = 1,
        ssd_memtable_flush_period: int = -1,
        ssd_memtable_flush_offset: int = -1,
        ssd_l0_files_per_compact: int = 4,
        ssd_rate_limit_mbps: int = 0,
        ssd_size_ratio: int = 10,
        ssd_compaction_trigger: int = 8,
        ssd_write_buffer_size: int = 2 * 1024 * 1024 * 1024,
        ssd_max_write_buffer_num: int = 16,
        ssd_cache_location: EmbeddingLocation = EmbeddingLocation.MANAGED,
        ssd_uniform_init_lower: float = -0.01,
        ssd_uniform_init_upper: float = 0.01,
        ssd_block_cache_size: int = 0,
        weights_precision: SparseType = SparseType.FP32,
        output_dtype: SparseType = SparseType.FP32,
        optimizer: OptimType = OptimType.EXACT_ROWWISE_ADAGRAD,
        # General Optimizer args
        stochastic_rounding: bool = True,
        gradient_clipping: bool = False,
        max_gradient: float = 1.0,
        max_norm: float = 0.0,
        learning_rate: float = 0.01,
        eps: float = 1.0e-8,  # used by Adagrad, LAMB, and Adam
        momentum: float = 0.9,  # used by LARS-SGD
        weight_decay: float = 0.0,  # used by LARS-SGD, LAMB, ADAM, and Rowwise Adagrad
        weight_decay_mode: WeightDecayMode = WeightDecayMode.NONE,  # used by Rowwise Adagrad
        eta: float = 0.001,  # used by LARS-SGD,
        beta1: float = 0.9,  # used by LAMB and ADAM
        beta2: float = 0.999,  # used by LAMB and ADAM
        counter_based_regularization: Optional[
            CounterBasedRegularizationDefinition
        ] = None,  # used by Rowwise Adagrad
        cowclip_regularization: Optional[
            CowClipDefinition
        ] = None,  # used by Rowwise Adagrad
        pooling_mode: PoolingMode = PoolingMode.SUM,
    ) -> None:
        super(SSDTableBatchedEmbeddingBags, self).__init__()

        self.pooling_mode = pooling_mode
        self.embedding_specs = embedding_specs
        (rows, dims) = zip(*embedding_specs)
        T_ = len(self.embedding_specs)
        assert T_ > 0
        # pyre-fixme[8]: Attribute has type `device`; used as `int`.
        self.current_device: torch.device = torch.cuda.current_device()

        self.feature_table_map: List[int] = (
            feature_table_map if feature_table_map is not None else list(range(T_))
        )
        T = len(self.feature_table_map)
        assert T_ <= T
        table_has_feature = [False] * T_
        for t in self.feature_table_map:
            table_has_feature[t] = True
        assert all(table_has_feature), "Each table must have at least one feature!"

        D_offsets = [dims[t] for t in self.feature_table_map]
        D_offsets = [0] + list(itertools.accumulate(D_offsets))
        self.total_D: int = D_offsets[-1]
        self.max_D: int = max(dims)
        self.register_buffer(
            "D_offsets",
            torch.tensor(D_offsets, device=self.current_device, dtype=torch.int32),
        )

        assert self.D_offsets.numel() == T + 1
        hash_size_cumsum = [0] + list(itertools.accumulate(rows))
        if hash_size_cumsum[-1] == 0:
            self.total_hash_size_bits: int = 0
        else:
            self.total_hash_size_bits: int = int(log2(float(hash_size_cumsum[-1])) + 1)
        # The last element is to easily access # of rows of each table by
        self.total_hash_size_bits = int(log2(float(hash_size_cumsum[-1])) + 1)
        self.total_hash_size: int = hash_size_cumsum[-1]
        # The last element is to easily access # of rows of each table by
        # hash_size_cumsum[t + 1] - hash_size_cumsum[t]
        hash_size_cumsum = [hash_size_cumsum[t] for t in self.feature_table_map] + [
            hash_size_cumsum[-1]
        ]
        self.register_buffer(
            "hash_size_cumsum",
            torch.tensor(
                hash_size_cumsum, device=self.current_device, dtype=torch.int64
            ),
        )

        element_size = 4
        cache_size = cache_sets * ASSOC * element_size * self.max_D
        logging.info(
            f"Using cache for SSD with admission algorithm "
            f"{CacheAlgorithm.LRU}, {cache_sets} sets, stored on {'DEVICE' if ssd_cache_location is EmbeddingLocation.DEVICE else 'MANAGED'} with {ssd_shards} shards, "
            f"Memtable Flush Period: {ssd_memtable_flush_period}, "
            f"Memtable Flush Offset: {ssd_memtable_flush_offset}, "
            f"Desired L0 files per compaction: {ssd_l0_files_per_compact}, "
            f"{cache_size / 1024.0 / 1024.0 / 1024.0 : .2f}GB"
        )
        self.register_buffer(
            "lxu_cache_state",
            torch.zeros(cache_sets, ASSOC, dtype=torch.int64).fill_(-1),
        )
        self.register_buffer(
            "lru_state", torch.zeros(cache_sets, ASSOC, dtype=torch.int64)
        )

        assert ssd_cache_location in (
            EmbeddingLocation.MANAGED,
            EmbeddingLocation.DEVICE,
        )

        assert weights_precision in (SparseType.FP32, SparseType.FP16)
        assert output_dtype in (SparseType.FP32, SparseType.FP16)
        self.weights_precision = weights_precision
        self.output_dtype: int = output_dtype.as_int()

        cache_dtype = weights_precision.as_dtype()
        if ssd_cache_location == EmbeddingLocation.MANAGED:
            self.register_buffer(
                "lxu_cache_weights",
                torch.ops.fbgemm.new_managed_tensor(
                    torch.zeros(
                        1,
                        device=self.current_device,
                        dtype=cache_dtype,
                    ),
                    [cache_sets * ASSOC, self.max_D],
                ),
            )
        else:
            self.register_buffer(
                "lxu_cache_weights",
                torch.zeros(
                    cache_sets * ASSOC,
                    self.max_D,
                    device=self.current_device,
                    dtype=cache_dtype,
                ),
            )

        self.timestep = 0

        import os

        os.makedirs(ssd_storage_directory, exist_ok=True)

        import tempfile

        ssd_directory = tempfile.mkdtemp(
            prefix="ssd_table_batched_embeddings", dir=ssd_storage_directory
        )
        # pyre-fixme[4]: Attribute must be annotated.
        # pyre-ignore[16]
        self.ssd_db = torch.classes.fbgemm.EmbeddingRocksDBWrapper(
            ssd_directory,
            ssd_shards,
            ssd_shards,
            ssd_memtable_flush_period,
            ssd_memtable_flush_offset,
            ssd_l0_files_per_compact,
            self.max_D,
            ssd_rate_limit_mbps,
            ssd_size_ratio,
            ssd_compaction_trigger,
            ssd_write_buffer_size,
            ssd_max_write_buffer_num,
            ssd_uniform_init_lower,
            ssd_uniform_init_upper,
            weights_precision.bit_rate(),  # row_storage_bitwidth
            ssd_block_cache_size,
        )
        # pyre-fixme[20]: Argument `self` expected.
        (low_priority, high_priority) = torch.cuda.Stream.priority_range()
        self.ssd_stream = torch.cuda.Stream(priority=low_priority)
        self.ssd_set_start = torch.cuda.Event()
        self.ssd_set_end = torch.cuda.Event()
        self.timesteps_prefetched: List[int] = []
        self.ssd_scratch_pads: List[Tuple[Tensor, Tensor, Tensor]] = []
        # TODO: add type annotation
        self.ssd_prefetch_data = []

        if weight_decay_mode == WeightDecayMode.COUNTER or counter_based_regularization:
            raise AssertionError(
                "weight_decay_mode = WeightDecayMode.COUNTER is not supported for SSD TBE."
            )
        counter_based_regularization = CounterBasedRegularizationDefinition()

        if weight_decay_mode == WeightDecayMode.COWCLIP or cowclip_regularization:
            raise AssertionError(
                "weight_decay_mode = WeightDecayMode.COWCLIP is not supported for SSD TBE."
            )
        cowclip_regularization = CowClipDefinition()

        self.optimizer_args = invokers.lookup_args_ssd.OptimizerArgs(
            stochastic_rounding=stochastic_rounding,
            gradient_clipping=gradient_clipping,
            max_gradient=max_gradient,
            max_norm=max_norm,
            learning_rate=learning_rate,
            eps=eps,
            beta1=beta1,
            beta2=beta2,
            weight_decay=weight_decay,
            weight_decay_mode=weight_decay_mode.value,
            eta=eta,
            momentum=momentum,
            counter_halflife=counter_based_regularization.counter_halflife,
            adjustment_iter=counter_based_regularization.adjustment_iter,
            adjustment_ub=counter_based_regularization.adjustment_ub,
            learning_rate_mode=counter_based_regularization.learning_rate_mode.value,
            grad_sum_decay=counter_based_regularization.grad_sum_decay.value,
            tail_id_threshold=counter_based_regularization.tail_id_threshold.val,
            is_tail_id_thresh_ratio=int(
                counter_based_regularization.tail_id_threshold.is_ratio
            ),
            total_hash_size=-1,  # Unused
            weight_norm_coefficient=cowclip_regularization.weight_norm_coefficient,
            lower_bound=cowclip_regularization.lower_bound,
            regularization_mode=weight_decay_mode.value,
        )

        table_embedding_dtype = weights_precision.as_dtype()

        self._apply_split(
            SplitState(
                dev_size=0,
                host_size=0,
                uvm_size=0,
                placements=[EmbeddingLocation.MANAGED_CACHING for _ in range(T_)],
                offsets=[0] * (len(rows)),
            ),
            "weights",
            dtype=table_embedding_dtype,
        )

        momentum1_offsets = [0] + list(itertools.accumulate(rows))
        self._apply_split(
            SplitState(
                dev_size=self.total_hash_size,
                host_size=0,
                uvm_size=0,
                placements=[EmbeddingLocation.DEVICE for _ in range(T_)],
                offsets=momentum1_offsets[:-1],
            ),
            "momentum1",
            dtype=torch.float32,
        )

        # add placeholder require_grad param to enable autograd without nn.parameter
        # this is needed to enable int8 embedding weights for SplitTableBatchedEmbedding
        self.placeholder_autograd_tensor = nn.Parameter(
            torch.zeros(0, device=self.current_device, dtype=torch.float)
        )

        # Register backward hook for evicting rows from a scratch pad to SSD
        # post backward
        self.placeholder_autograd_tensor.register_hook(self._evict_from_scratch_pad)

        assert optimizer in (
            OptimType.EXACT_ROWWISE_ADAGRAD,
        ), f"Optimizer {optimizer} is not supported by SSDTableBatchedEmbeddingBags"
        self.optimizer = optimizer

    def _apply_split(
        self,
        split: SplitState,
        prefix: str,
        dtype: Type[torch.dtype],
        enforce_hbm: bool = False,
        make_dev_param: bool = False,
        dev_reshape: Optional[Tuple[int, ...]] = None,
    ) -> None:
        apply_split_helper(
            self.register_buffer,
            functools.partial(setattr, self),
            self.current_device,
            False,  # use_cpu
            self.feature_table_map,
            split,
            prefix,
            dtype,
            enforce_hbm,
            make_dev_param,
            dev_reshape,
        )

    def to_pinned_cpu(self, t: torch.Tensor) -> torch.Tensor:
        t_cpu = torch.empty(t.shape, pin_memory=True, dtype=t.dtype)
        t_cpu.copy_(t, non_blocking=True)
        return t_cpu

    def evict(
        self, evicted_rows: Tensor, evicted_indices: Tensor, actions_count_cpu: Tensor
    ) -> None:
        """
        Evict data from the given input tensors to SSD via RocksDB
        """
        with torch.cuda.stream(self.ssd_stream):
            self.ssd_stream.wait_event(self.ssd_set_start)
            evicted_rows_cpu = self.to_pinned_cpu(evicted_rows)
            evicted_indices_cpu = self.to_pinned_cpu(evicted_indices)
            evicted_rows.record_stream(self.ssd_stream)
            evicted_indices.record_stream(self.ssd_stream)
            self.ssd_db.set_cuda(
                evicted_indices_cpu, evicted_rows_cpu, actions_count_cpu, self.timestep
            )
            # TODO: is this needed?
            # Need a way to synchronize
            #  actions_count_cpu.record_stream(self.ssd_stream)
            self.ssd_stream.record_event(self.ssd_set_end)

    def _evict_from_scratch_pad(self, grad: Tensor) -> None:
        assert len(self.ssd_scratch_pads) > 0, "There must be at least one scratch pad"
        (inserted_rows_gpu, post_bwd_evicted_indices, actions_count_cpu) = (
            self.ssd_scratch_pads.pop(0)
        )
        self.evict(inserted_rows_gpu, post_bwd_evicted_indices, actions_count_cpu)

    def _compute_cache_ptrs(
        self,
        linear_cache_indices: torch.Tensor,
        assigned_cache_slots: torch.Tensor,
        linear_index_inverse_indices: torch.Tensor,
        unique_indices_count_cumsum: torch.Tensor,
        cache_set_inverse_indices: torch.Tensor,
        inserted_rows_gpu: torch.Tensor,
        unique_indices_length: torch.Tensor,
        inserted_indices: torch.Tensor,
        actions_count_cpu: torch.Tensor,
    ) -> torch.Tensor:
        lxu_cache_locations = torch.ops.fbgemm.lxu_cache_lookup(
            linear_cache_indices,
            self.lxu_cache_state,
            self.hash_size_cumsum[-1].item(),
        )
        lxu_cache_ptrs, post_bwd_evicted_indices = (
            torch.ops.fbgemm.ssd_generate_row_addrs(
                lxu_cache_locations,
                assigned_cache_slots,
                linear_index_inverse_indices,
                unique_indices_count_cumsum,
                cache_set_inverse_indices,
                self.lxu_cache_weights,
                inserted_rows_gpu,
                unique_indices_length,
                inserted_indices,
            )
        )
        # Store scratch pad info for post backward eviction
        self.ssd_scratch_pads.append(
            (inserted_rows_gpu, post_bwd_evicted_indices, actions_count_cpu)
        )
        assert lxu_cache_ptrs[lxu_cache_ptrs == 0].numel() == 0
        return (
            lxu_cache_ptrs,
            inserted_rows_gpu,
            post_bwd_evicted_indices,
            actions_count_cpu,
        )

    def prefetch(self, indices: Tensor, offsets: Tensor) -> Optional[Tensor]:
        (indices, offsets) = indices.long(), offsets.long()

        linear_cache_indices = torch.ops.fbgemm.linearize_cache_indices(
            self.hash_size_cumsum,
            indices,
            offsets,
        )
        self.timestep += 1
        self.timesteps_prefetched.append(self.timestep)
        (
            inserted_indices,
            evicted_indices,
            assigned_cache_slots,
            actions_count_gpu,
            linear_index_inverse_indices,
            unique_indices_count_cumsum,
            cache_set_inverse_indices,
            unique_indices_length,
        ) = torch.ops.fbgemm.ssd_cache_populate_actions(
            linear_cache_indices,
            self.total_hash_size,
            self.lxu_cache_state,
            self.timestep,
            1,  # for now assume prefetch_dist == 1
            self.lru_state,
        )

        actions_count_cpu = self.to_pinned_cpu(actions_count_gpu)
        assigned_cache_slots = assigned_cache_slots.long()
        evicted_rows = self.lxu_cache_weights[
            assigned_cache_slots.clamp(min=0).long(), :
        ]
        inserted_rows = torch.empty(
            evicted_rows.shape,
            dtype=self.lxu_cache_weights.dtype,
            pin_memory=True,
        )

        current_stream = torch.cuda.current_stream()

        # Ensure the previous iterations l3_db.set(..) has completed.
        current_stream.wait_event(self.ssd_set_end)
        self.ssd_db.get_cuda(
            self.to_pinned_cpu(inserted_indices), inserted_rows, actions_count_cpu
        )
        current_stream.record_event(self.ssd_set_start)
        # TODO: T123943415 T123943414 this is a big copy that is (mostly) unnecessary with a decent cache hit rate.
        # Should we allocate on HBM?
        inserted_rows_gpu = inserted_rows.cuda(non_blocking=True)

        torch.ops.fbgemm.masked_index_put(
            self.lxu_cache_weights,
            assigned_cache_slots,
            inserted_rows_gpu,
            actions_count_gpu,
        )

        # Evict rows from cache to SSD
        self.evict(evicted_rows, evicted_indices, actions_count_cpu)

        # TODO: keep only necessary tensors
        self.ssd_prefetch_data.append(
            (
                linear_cache_indices,
                assigned_cache_slots,
                linear_index_inverse_indices,
                unique_indices_count_cumsum,
                cache_set_inverse_indices,
                inserted_rows_gpu,
                unique_indices_length,
                inserted_indices,
                actions_count_cpu,
            )
        )

    def forward(
        self,
        indices: Tensor,
        offsets: Tensor,
        per_sample_weights: Optional[Tensor] = None,
        feature_requires_grad: Optional[Tensor] = None,
    ) -> Tensor:
        (indices, offsets) = indices.long(), offsets.long()
        # Force casting per_sample_weights to float
        if per_sample_weights is not None:
            per_sample_weights = per_sample_weights.float()
        if len(self.timesteps_prefetched) == 0:
            with record_function("## prefetch ##"):
                self.prefetch(indices, offsets)
        assert len(self.ssd_prefetch_data) > 0

        prefetch_data = self.ssd_prefetch_data.pop(0)
        (
            lxu_cache_ptrs,
            inserted_rows_gpu,
            post_bwd_evicted_indices,
            actions_count_cpu,
        ) = self._compute_cache_ptrs(*prefetch_data)

        common_args = invokers.lookup_args_ssd.CommonArgs(
            placeholder_autograd_tensor=self.placeholder_autograd_tensor,
            output_dtype=self.output_dtype,
            dev_weights=self.weights_dev,
            host_weights=self.weights_host,
            uvm_weights=self.weights_uvm,
            lxu_cache_weights=self.lxu_cache_weights,
            weights_placements=self.weights_placements,
            weights_offsets=self.weights_offsets,
            D_offsets=self.D_offsets,
            total_D=self.total_D,
            max_D=self.max_D,
            hash_size_cumsum=self.hash_size_cumsum,
            total_hash_size_bits=self.total_hash_size_bits,
            indices=indices,
            offsets=offsets,
            pooling_mode=self.pooling_mode,
            indice_weights=per_sample_weights,
            feature_requires_grad=feature_requires_grad,
            lxu_cache_locations=lxu_cache_ptrs,
            uvm_cache_stats=None,
            vbe_metadata=invokers.lookup_args_ssd.VBEMetadata(
                B_offsets=None,
                output_offsets_feature_rank=None,
                B_offsets_rank_per_feature=None,
                max_B=-1,
                max_B_feature_rank=-1,
                output_size=-1,
            ),
            # Unused arguments
            is_experimental=False,
            use_uniq_cache_locations_bwd=False,
            use_homogeneous_placements=True,
            # The keys for ssd_tensors are controlled by ssd_tensors in
            # codegen/genscript/optimizer_args.py
            ssd_tensors={
                "row_addrs": lxu_cache_ptrs,
                "inserted_rows": inserted_rows_gpu,
                "post_bwd_evicted_indices": post_bwd_evicted_indices,
                "actions_count": actions_count_cpu,
            },
        )

        self.timesteps_prefetched.pop(0)

        if self.optimizer == OptimType.EXACT_SGD:
            raise AssertionError(
                "SSDTableBatchedEmbeddingBags currently does not support SGD"
            )
            return invokers.lookup_sgd_ssd.invoke(common_args, self.optimizer_args)

        momentum1 = invokers.lookup_args_ssd.Momentum(
            dev=self.momentum1_dev,
            host=self.momentum1_host,
            uvm=self.momentum1_uvm,
            offsets=self.momentum1_offsets,
            placements=self.momentum1_placements,
        )

        if self.optimizer == OptimType.EXACT_ROWWISE_ADAGRAD:
            return invokers.lookup_rowwise_adagrad_ssd.invoke(
                common_args, self.optimizer_args, momentum1
            )

    @torch.jit.ignore
    def debug_split_optimizer_states(self) -> List[Tuple[torch.Tensor]]:
        """
        Returns a list of states, split by table
        Testing only
        """
        (rows, _) = zip(*self.embedding_specs)

        rows_cumsum = [0] + list(itertools.accumulate(rows))

        return [
            (
                self.momentum1_dev.detach()[rows_cumsum[t] : rows_cumsum[t + 1]].view(
                    row
                ),
            )
            for t, row in enumerate(rows)
        ]

    @torch.jit.export
    def debug_split_embedding_weights(self) -> List[torch.Tensor]:
        """
        Returns a list of weights, split by table.

        Testing only, very slow.
        """
        (rows, _) = zip(*self.embedding_specs)

        rows_cumsum = [0] + list(itertools.accumulate(rows))
        splits = []
        for t, (row, dim) in enumerate(self.embedding_specs):
            weights = torch.empty((row, dim), dtype=self.weights_precision.as_dtype())
            self.ssd_db.get_cuda(
                torch.arange(rows_cumsum[t], rows_cumsum[t + 1]).to(torch.int64),
                weights,
                torch.as_tensor([row]),
            )
            splits.append(weights)
        torch.cuda.synchronize(self.current_device)
        return splits

    @torch.jit.export
    def set_learning_rate(self, lr: float) -> None:
        """
        Sets the learning rate.
        """
        self._set_learning_rate(lr)

    @torch.jit.ignore
    def _set_learning_rate(self, lr: float) -> float:
        """
        Helper function to script `set_learning_rate`.
        Note that returning None does not work.
        """
        self.optimizer_args = self.optimizer_args._replace(learning_rate=lr)
        return 0.0

    def flush(self) -> None:
        active_slots_mask = self.lxu_cache_state != -1
        active_weights = self.lxu_cache_weights.masked_select(
            active_slots_mask.view(-1, 1)
        ).view(-1, self.max_D)
        active_ids = self.lxu_cache_state.view(-1).masked_select(
            active_slots_mask.view(-1)
        )
        torch.cuda.current_stream().wait_stream(self.ssd_stream)
        self.ssd_db.set_cuda(
            active_ids.cpu(),
            active_weights.cpu(),
            torch.tensor([active_ids.numel()]),
            self.timestep,
        )


class SSDIntNBitTableBatchedEmbeddingBags(nn.Module):
    """
    SSD Table-batched version of nn.EmbeddingBag(sparse=False)
    Inference version, with FP32/FP16/FP8/INT8/INT4/INT2 supports
    """

    embedding_specs: List[Tuple[str, int, int, SparseType]]

    def __init__(
        self,
        embedding_specs: List[
            Tuple[str, int, int, SparseType]
        ],  # tuple of (feature_names, rows, dims, SparseType)
        feature_table_map: Optional[List[int]] = None,  # [T]
        pooling_mode: PoolingMode = PoolingMode.SUM,
        output_dtype: SparseType = SparseType.FP16,
        row_alignment: Optional[int] = None,
        fp8_exponent_bits: Optional[int] = None,
        fp8_exponent_bias: Optional[int] = None,
        cache_assoc: int = 32,
        scale_bias_size_in_bytes: int = DEFAULT_SCALE_BIAS_SIZE_IN_BYTES,
        cache_sets: int = 0,
        ssd_storage_directory: str = "/tmp",
        ssd_shards: int = 1,
        ssd_memtable_flush_period: int = -1,
        ssd_memtable_flush_offset: int = -1,
        ssd_l0_files_per_compact: int = 4,
        ssd_rate_limit_mbps: int = 0,
        ssd_size_ratio: int = 10,
        ssd_compaction_trigger: int = 8,
        ssd_write_buffer_size: int = 2 * 1024 * 1024 * 1024,
        ssd_max_write_buffer_num: int = 16,
        ssd_cache_location: EmbeddingLocation = EmbeddingLocation.MANAGED,
        ssd_uniform_init_lower: float = -0.01,
        ssd_uniform_init_upper: float = 0.01,
    ) -> None:  # noqa C901  # tuple of (rows, dims,)
        super(SSDIntNBitTableBatchedEmbeddingBags, self).__init__()

        assert cache_assoc == 32, "Only 32-way cache is supported now"

        self.scale_bias_size_in_bytes = scale_bias_size_in_bytes
        self.pooling_mode = pooling_mode
        self.embedding_specs = embedding_specs
        T_ = len(self.embedding_specs)
        assert T_ > 0
        device = torch.cuda.current_device()
        if device is None:
            self.current_device: torch.device = torch.device(
                torch.cuda.current_device()
            )
        elif isinstance(device, torch.device):
            self.current_device = device
        else:
            self.current_device = torch.device(device)
        self.use_cpu: bool = self.current_device.type == "cpu"

        self.feature_table_map: List[int] = (
            feature_table_map if feature_table_map is not None else list(range(T_))
        )
        T = len(self.feature_table_map)
        assert T_ <= T
        table_has_feature = [False] * T_
        for t in self.feature_table_map:
            table_has_feature[t] = True
        assert all(table_has_feature), "Each table must have at least one feature!"

        self.output_dtype: int = output_dtype.as_int()
        # (feature_names, rows, dims, weights_tys) = zip(*embedding_specs)
        # Pyre workaround
        rows: List[int] = [e[1] for e in embedding_specs]
        dims: List[int] = [e[2] for e in embedding_specs]
        weights_tys: List[SparseType] = [e[3] for e in embedding_specs]

        D_offsets = [dims[t] for t in self.feature_table_map]
        D_offsets = [0] + list(itertools.accumulate(D_offsets))
        self.total_D: int = D_offsets[-1]
        self.register_buffer(
            "D_offsets",
            torch.tensor(D_offsets, device=self.current_device, dtype=torch.int32),
        )

        if row_alignment is None:
            self.row_alignment: int = 1 if self.use_cpu else 16
        else:
            self.row_alignment = row_alignment

        for dim, weight_ty in zip(dims, weights_tys):
            if not weight_ty.is_float():
                assert (
                    dim % (8 / weight_ty.bit_rate()) == 0
                ), f"For quantized types we need to at least pack at byte granularity, dim: {dim}, weight_ty: {weight_ty}"

        def max_ty_D(ty: SparseType) -> int:
            return max(
                [dim for dim, weight_ty in zip(dims, weights_tys) if weight_ty == ty],
                default=0,
            )

        self.max_int2_D: int = max_ty_D(SparseType.INT2)
        self.max_int4_D: int = max_ty_D(SparseType.INT4)
        self.max_int8_D: int = max_ty_D(SparseType.INT8)
        self.max_float8_D: int = max_ty_D(SparseType.FP8)
        self.max_float16_D: int = max_ty_D(SparseType.FP16)
        self.max_float32_D: int = max_ty_D(SparseType.FP32)

        cached_dims = [
            rounded_row_size_in_bytes(
                embedding_spec[2], embedding_spec[3], 16, self.scale_bias_size_in_bytes
            )
            for embedding_spec in self.embedding_specs
        ]
        self.max_D_cache: int = max(cached_dims) if len(cached_dims) > 0 else 0

        placements = []
        offsets = []
        uvm_size = 0
        for _, num_embeddings, embedding_dim, weight_ty in embedding_specs:
            embedding_dim = rounded_row_size_in_bytes(
                embedding_dim, weight_ty, self.row_alignment, scale_bias_size_in_bytes
            )
            state_size = num_embeddings * embedding_dim
            state_size = align_to_cacheline(state_size)
            placements.append(EmbeddingLocation.MANAGED_CACHING)
            offsets.append(uvm_size)
            uvm_size += state_size

        self.weights_physical_offsets: List[int] = offsets

        weights_tys_int = [weights_tys[t].as_int() for t in self.feature_table_map]
        self.register_buffer(
            "weights_tys",
            torch.tensor(
                weights_tys_int, device=self.current_device, dtype=torch.uint8
            ),
        )
        self.weight_initialized: bool = True

        assert self.D_offsets.numel() == T + 1
        hash_size_cumsum = [0] + list(itertools.accumulate(rows))
        if hash_size_cumsum[-1] == 0:
            self.total_hash_size_bits: int = 0
        else:
            self.total_hash_size_bits: int = int(log2(float(hash_size_cumsum[-1])) + 1)
        # The last element is to easily access # of rows of each table by
        self.total_hash_size_bits = int(log2(float(hash_size_cumsum[-1])) + 1)
        self.total_hash_size: int = hash_size_cumsum[-1]
        # The last element is to easily access # of rows of each table by
        # hash_size_cumsum[t + 1] - hash_size_cumsum[t]
        hash_size_cumsum = [hash_size_cumsum[t] for t in self.feature_table_map] + [
            hash_size_cumsum[-1]
        ]
        self.register_buffer(
            "hash_size_cumsum",
            torch.tensor(
                hash_size_cumsum, device=self.current_device, dtype=torch.int64
            ),
        )
        element_size = 1
        cache_size = cache_sets * ASSOC * element_size * self.max_D_cache
        logging.info(
            f"Using cache for SSD with admission algorithm "
            f"{CacheAlgorithm.LRU}, {cache_sets} sets, stored on {'DEVICE' if ssd_cache_location is EmbeddingLocation.DEVICE else 'MANAGED'} with {ssd_shards} shards, "
            f"Memtable Flush Period: {ssd_memtable_flush_period}, "
            f"Memtable Flush Offset: {ssd_memtable_flush_offset}, "
            f"Desired L0 files per compaction: {ssd_l0_files_per_compact}, "
            f"{cache_size / 1024.0 / 1024.0 / 1024.0 : .2f}GB"
        )
        self.register_buffer(
            "lxu_cache_state",
            torch.zeros(cache_sets, ASSOC, dtype=torch.int64).fill_(-1),
        )
        self.register_buffer(
            "lru_state", torch.zeros(cache_sets, ASSOC, dtype=torch.int64)
        )

        assert ssd_cache_location in (
            EmbeddingLocation.MANAGED,
            EmbeddingLocation.DEVICE,
        )
        if ssd_cache_location == EmbeddingLocation.MANAGED:
            self.register_buffer(
                "lxu_cache_weights",
                torch.ops.fbgemm.new_managed_tensor(
                    torch.zeros(1, device=self.current_device, dtype=torch.uint8),
                    [cache_sets * ASSOC, self.max_D_cache],
                ),
            )
        else:
            self.register_buffer(
                "lxu_cache_weights",
                torch.zeros(
                    cache_sets * ASSOC,
                    self.max_D_cache,
                    device=self.current_device,
                    dtype=torch.uint8,
                ),
            )

        import os

        os.makedirs(ssd_storage_directory, exist_ok=True)

        import tempfile

        ssd_directory = tempfile.mkdtemp(
            prefix="ssd_table_batched_embeddings", dir=ssd_storage_directory
        )
        # pyre-fixme[4]: Attribute must be annotated.
        # pyre-ignore[16]
        self.ssd_db = torch.classes.fbgemm.EmbeddingRocksDBWrapper(
            ssd_directory,
            ssd_shards,
            ssd_shards,
            ssd_memtable_flush_period,
            ssd_memtable_flush_offset,
            ssd_l0_files_per_compact,
            self.max_D_cache,
            ssd_rate_limit_mbps,
            ssd_size_ratio,
            ssd_compaction_trigger,
            ssd_write_buffer_size,
            ssd_max_write_buffer_num,
            ssd_uniform_init_lower,
            ssd_uniform_init_upper,
            8,  # row_storage_bitwidth
            0,  # ssd_block_cache_size
        )
        # pyre-fixme[20]: Argument `self` expected.
        (low_priority, high_priority) = torch.cuda.Stream.priority_range()
        self.ssd_stream = torch.cuda.Stream(priority=low_priority)
        self.ssd_set_start = torch.cuda.Event()
        self.ssd_set_end = torch.cuda.Event()

        # pyre-fixme[4]: Attribute must be annotated.
        # pyre-ignore[16]
        self.timestep_counter = torch.classes.fbgemm.AtomicCounter()
        # pyre-fixme[4]: Attribute must be annotated.
        # pyre-ignore[16]
        self.timestep_prefetch_size = torch.classes.fbgemm.AtomicCounter()

        self.weights_dev: torch.Tensor = torch.empty(
            0,
            device=self.current_device,
            dtype=torch.uint8,
        )
        self.register_buffer(
            "weights_uvm",
            torch.tensor((0,), device=self.current_device, dtype=torch.uint8),
        )
        self.register_buffer(
            "weights_host",
            torch.empty(0),
        )

        self.register_buffer(
            "weights_placements",
            torch.tensor(
                [EmbeddingLocation.MANAGED_CACHING for _ in range(T_)],
                dtype=torch.int32,
            ),
        )
        weights_offsets = [0] + list(
            itertools.accumulate([row * dim for (row, dim) in zip(rows, dims)])
        )
        self.register_buffer(
            "weights_offsets",
            torch.tensor(
                weights_offsets[:-1],
                device=self.current_device,
                dtype=torch.int64,
            ),
        )

        if self.max_float8_D > 0:
            default_config = SparseType.FP8.default_config()
            self.fp8_exponent_bits: int = (
                default_config.get("exponent_bits")
                if fp8_exponent_bits is None
                else fp8_exponent_bits
            )
            self.fp8_exponent_bias: int = (
                default_config.get("exponent_bias")
                if fp8_exponent_bias is None
                else fp8_exponent_bias
            )
        else:
            self.fp8_exponent_bits = -1
            self.fp8_exponent_bias = -1

    @torch.jit.export
    def prefetch(self, indices: Tensor, offsets: Tensor) -> Tensor:
        (indices, offsets) = indices.long(), offsets.long()
        linear_cache_indices = torch.ops.fbgemm.linearize_cache_indices(
            self.hash_size_cumsum,
            indices,
            offsets,
        )
        self.timestep_counter.increment()
        self.timestep_prefetch_size.increment()
        (
            inserted_indices,
            evicted_indices,
            assigned_cache_slots,
            actions_count_gpu,
            _,
            _,
            _,
            _,
        ) = torch.ops.fbgemm.ssd_cache_populate_actions(
            linear_cache_indices,
            self.total_hash_size,
            self.lxu_cache_state,
            self.timestep_counter.get(),
            1,  # for now assume prefetch_dist == 1
            self.lru_state,
        )
        actions_count_cpu = torch.empty(
            actions_count_gpu.shape, pin_memory=True, dtype=actions_count_gpu.dtype
        )
        actions_count_cpu.copy_(actions_count_gpu, non_blocking=True)
        assigned_cache_slots = assigned_cache_slots.long()
        evicted_rows = self.lxu_cache_weights[
            assigned_cache_slots.clamp_(min=0).long(), :
        ]
        inserted_rows = torch.empty(
            evicted_rows.shape,
            dtype=self.lxu_cache_weights.dtype,
            pin_memory=True,
        )

        current_stream = torch.cuda.current_stream()

        # Ensure the previous iterations l3_db.set(..) has completed.
        current_stream.wait_event(self.ssd_set_end)
        inserted_indices_cpu = torch.empty(
            inserted_indices.shape, pin_memory=True, dtype=inserted_indices.dtype
        )
        inserted_indices_cpu.copy_(inserted_indices, non_blocking=True)
        self.ssd_db.get_cuda(
            inserted_indices_cpu,
            inserted_rows,
            actions_count_cpu,
        )
        current_stream.record_event(self.ssd_set_start)
        # TODO: T123943415 T123943414 this is a big copy that is (mostly) unnecessary with a decent cache hit rate.
        # Should we allocate on HBM?
        inserted_rows_gpu = inserted_rows.to(self.current_device, non_blocking=True)

        # self.lxu_cache_weights[assigned_cache_slots, :] = inserted_rows.cuda(non_blocking=True)
        torch.ops.fbgemm.masked_index_put(
            self.lxu_cache_weights,
            assigned_cache_slots,
            inserted_rows_gpu,
            actions_count_gpu,
        )

        with torch.cuda.stream(self.ssd_stream):
            self.ssd_stream.wait_event(self.ssd_set_start)
            evicted_rows_cpu = torch.empty(
                evicted_rows.shape, pin_memory=True, dtype=evicted_rows.dtype
            )
            evicted_rows_cpu.copy_(evicted_rows, non_blocking=True)
            evicted_indices_cpu = torch.empty(
                evicted_indices.shape, pin_memory=True, dtype=evicted_indices.dtype
            )
            evicted_indices_cpu.copy_(evicted_indices, non_blocking=True)
            evicted_rows.record_stream(self.ssd_stream)
            evicted_indices.record_stream(self.ssd_stream)
            self.ssd_db.set_cuda(
                evicted_indices_cpu,
                evicted_rows_cpu,
                actions_count_cpu,
                self.timestep_counter.get(),
            )
            # TODO: is this needed?
            # Need a way to synchronize
            #  actions_count_cpu.record_stream(self.ssd_stream)
            self.ssd_stream.record_event(self.ssd_set_end)
        return linear_cache_indices

    def forward(
        self,
        indices: Tensor,
        offsets: Tensor,
        per_sample_weights: Optional[Tensor] = None,
    ) -> Tensor:
        if self.timestep_prefetch_size.get() <= 0:
            with record_function("## prefetch ##"):
                linear_cache_indices = self.prefetch(indices, offsets)
        else:
            linear_cache_indices = torch.ops.fbgemm.linearize_cache_indices(
                self.hash_size_cumsum,
                indices,
                offsets,
            )
        lxu_cache_locations = torch.ops.fbgemm.lxu_cache_lookup(
            linear_cache_indices,
            self.lxu_cache_state,
            self.hash_size_cumsum[-1].item(),
        )

        self.timestep_prefetch_size.decrement()

        assert (
            self.weight_initialized
        ), "weight needs to be initialized before forward function"

        # Note: CPU and CUDA ops use the same interface to facilitate JIT IR
        # generation for CUDA/CPU. For CPU op, we don't need weights_uvm and
        # weights_placements
        return torch.ops.fbgemm.int_nbit_split_embedding_codegen_lookup_function(
            dev_weights=self.weights_dev,
            uvm_weights=self.weights_uvm,
            weights_placements=self.weights_placements,
            weights_offsets=self.weights_offsets,
            weights_tys=self.weights_tys,
            D_offsets=self.D_offsets,
            total_D=self.total_D,
            max_int2_D=self.max_int2_D,
            max_int4_D=self.max_int4_D,
            max_int8_D=self.max_int8_D,
            max_float16_D=self.max_float16_D,
            max_float32_D=self.max_float32_D,
            indices=indices,
            offsets=offsets,
            pooling_mode=int(self.pooling_mode),
            indice_weights=per_sample_weights,
            output_dtype=self.output_dtype,
            lxu_cache_weights=self.lxu_cache_weights,
            lxu_cache_locations=lxu_cache_locations,
            row_alignment=self.row_alignment,
            max_float8_D=self.max_float8_D,
            fp8_exponent_bits=self.fp8_exponent_bits,
            fp8_exponent_bias=self.fp8_exponent_bias,
        )

    @torch.jit.export
    def split_embedding_weights(
        self, split_scale_shifts: bool = True
    ) -> List[Tuple[Tensor, Optional[Tensor]]]:
        """
        Returns a list of weights, split by table.

        Testing only, very slow.
        """
        splits: List[Tuple[Tensor, Optional[Tensor]]] = []
        rows_cumsum = 0
        for _, row, dim, weight_ty in self.embedding_specs:
            weights = torch.empty(
                (
                    row,
                    rounded_row_size_in_bytes(
                        dim,
                        weight_ty,
                        self.row_alignment,
                        self.scale_bias_size_in_bytes,
                    ),
                ),
                dtype=torch.uint8,
            )
            self.ssd_db.get_cuda(
                torch.arange(rows_cumsum, rows_cumsum + row).to(torch.int64),
                weights,
                torch.as_tensor([row]),
            )
            rows_cumsum += row
            torch.cuda.synchronize(self.current_device)

            weights_shifts = weights.detach()

            if split_scale_shifts:
                # remove the padding at the end of each row.
                weights_shifts = weights_shifts[
                    :,
                    : unpadded_row_size_in_bytes(
                        dim, weight_ty, self.scale_bias_size_in_bytes
                    ),
                ]
                if (
                    weight_ty == SparseType.INT8
                    or weight_ty == SparseType.INT4
                    or weight_ty == SparseType.INT2
                ):
                    splits.append(
                        (
                            weights_shifts[:, self.scale_bias_size_in_bytes :],
                            weights_shifts[:, : self.scale_bias_size_in_bytes],
                        )
                    )
                else:
                    assert (
                        weight_ty == SparseType.FP8
                        or weight_ty == SparseType.FP16
                        or weight_ty == SparseType.FP32
                    )
                    splits.append(
                        (
                            weights_shifts,
                            None,
                        )
                    )
            else:
                splits.append((weights_shifts, None))

        torch.cuda.synchronize(self.current_device)
        return splits
