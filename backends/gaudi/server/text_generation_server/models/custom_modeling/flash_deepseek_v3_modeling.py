# coding=utf-8
# Copyright 2023, 2024 DeepSeek-AI and The HuggingFace Inc. team. All rights reserved.
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

from typing import List, Optional, Tuple, Type

import torch
import torch.distributed
from torch import nn
from transformers.activations import ACT2FN
from transformers.configuration_utils import PretrainedConfig

from text_generation_server.layers import (
    FastLinear,
    SpeculativeHead,
    TensorParallelColumnLinear,
    TensorParallelEmbedding,
    TensorParallelRowLinear,
    get_linear,
    Fp8Linear,
)
from text_generation_server.layers.attention import (
    Seqlen,
    attention,
    paged_attention_mla,
    set_block_mapping,
    HPUPagedAttentionMetadata,
)
from text_generation_server.layers.attention.kv_cache import KVCache, get_kv_scales
from text_generation_server.layers.layernorm import FastRMSNorm
from text_generation_server.layers.moe import DenseMoELayer, MoELayer, SparseMoELayer
from text_generation_server.layers.rotary import PositionRotaryEmbedding, get_mscale
from text_generation_server.utils.weights import Weights
import habana_frameworks.torch as htorch


def get_and_maybe_dequant_weights(layer: torch.nn.Module) -> torch.Tensor:
    if isinstance(layer, Fp8Linear):
        eye = torch.eye(
            layer.qweight.shape[-1], dtype=torch.bfloat16, device=layer.qweight.device
        )
        dequant_weights = layer(eye)
        del eye
        # standardize to (output, input)
        return dequant_weights.T
    return layer.weight


class DeepseekV3Config(PretrainedConfig):
    def __init__(
        self,
        vocab_size=102400,
        hidden_size=4096,
        intermediate_size=11008,
        moe_intermediate_size=1407,
        num_hidden_layers=30,
        num_attention_heads=32,
        num_key_value_heads=32,
        n_shared_experts=2,
        n_routed_experts=160,
        ep_size=1,
        routed_scaling_factor=1.0,
        kv_lora_rank=512,
        q_lora_rank=1536,
        qk_rope_head_dim=64,
        v_head_dim=128,
        qk_nope_head_dim=128,
        topk_method="gready",
        n_group=8,
        topk_group=3,
        num_experts_per_tok=6,
        moe_layer_freq=1,
        first_k_dense_replace=0,
        norm_topk_prob=False,
        scoring_func="softmax",
        aux_loss_alpha=0.001,
        seq_aux=True,
        hidden_act="silu",
        max_position_embeddings=2048,
        initializer_range=0.02,
        rms_norm_eps=1e-6,
        use_cache=True,
        pad_token_id=None,
        bos_token_id=100000,
        eos_token_id=100001,
        pretraining_tp=1,
        tie_word_embeddings=False,
        rope_theta=10000.0,
        rope_scaling=None,
        attention_bias=False,
        attention_dropout=0.0,
        **kwargs,
    ):
        self.vocab_size = vocab_size
        self.max_position_embeddings = max_position_embeddings
        self.hidden_size = hidden_size
        self.intermediate_size = intermediate_size
        self.moe_intermediate_size = moe_intermediate_size
        self.num_hidden_layers = num_hidden_layers
        self.num_attention_heads = num_attention_heads
        self.n_shared_experts = n_shared_experts
        self.n_routed_experts = n_routed_experts
        self.ep_size = ep_size
        self.routed_scaling_factor = routed_scaling_factor
        self.kv_lora_rank = kv_lora_rank
        self.q_lora_rank = q_lora_rank
        self.qk_rope_head_dim = qk_rope_head_dim
        self.v_head_dim = v_head_dim
        self.qk_nope_head_dim = qk_nope_head_dim
        self.topk_method = topk_method
        self.n_group = n_group
        self.topk_group = topk_group
        self.num_experts_per_tok = num_experts_per_tok
        self.moe_layer_freq = moe_layer_freq
        self.first_k_dense_replace = first_k_dense_replace
        self.norm_topk_prob = norm_topk_prob
        self.scoring_func = scoring_func
        self.aux_loss_alpha = aux_loss_alpha
        self.seq_aux = seq_aux
        # for backward compatibility
        if num_key_value_heads is None:
            num_key_value_heads = num_attention_heads

        self.num_key_value_heads = num_key_value_heads
        self.hidden_act = hidden_act
        self.initializer_range = initializer_range
        self.rms_norm_eps = rms_norm_eps
        self.pretraining_tp = pretraining_tp
        self.use_cache = use_cache
        self.rope_theta = rope_theta
        self.rope_scaling = rope_scaling
        self.attention_bias = attention_bias
        self.attention_dropout = attention_dropout

        tie_word_embeddings = kwargs.pop("tie_word_embeddings", False)
        if tie_word_embeddings:
            raise ValueError(
                "tie_word_embeddings is not supported for Deepseek V2 models."
            )

        if ep_size != 1:
            raise ValueError(
                f"Currently only ep_size == 1 is supported for Deepseek V2 models, was {ep_size}"
            )

        super().__init__(
            pad_token_id=pad_token_id,
            bos_token_id=bos_token_id,
            eos_token_id=eos_token_id,
            tie_word_embeddings=tie_word_embeddings,
            **kwargs,
        )


class DeepseekV3Attention(torch.nn.Module):
    def __init__(
        self,
        prefix: str,
        config,
        weights: Weights,
        rotary_emb,
    ):
        super().__init__()
        self.num_heads = config.num_attention_heads
        self.hidden_size = config.hidden_size
        self.kv_lora_rank = config.kv_lora_rank
        self.q_lora_rank = config.q_lora_rank
        self.qk_nope_head_dim = config.qk_nope_head_dim
        self.qk_rope_head_dim = config.qk_rope_head_dim
        self.head_size = config.qk_nope_head_dim + config.qk_rope_head_dim
        self.value_head_size = config.v_head_dim
        self.head_pad_size = max(self.head_size, self.value_head_size)
        self.rotary_emb = rotary_emb

        mscale = get_mscale(
            self.rotary_emb.scaling_factor, self.rotary_emb.mscale_all_dim
        )
        self.softmax_scale = self.head_size**-0.5 * mscale * mscale

        if self.num_heads % weights.process_group.size() != 0:
            raise ValueError(
                f"`num_heads` must be divisible by `num_shards` (got `num_heads`: {self.num_heads} "
                f"and `num_shards`: {weights.process_group.size()}"
            )
        self.num_heads = self.num_heads // weights.process_group.size()
        self.num_key_value_heads = (
            config.num_key_value_heads // weights.process_group.size()
        )

        if self.q_lora_rank is None:
            self.q_proj = TensorParallelColumnLinear.load(
                config,
                prefix=f"{prefix}.q_proj",
                weights=weights,
                bias=config.attention_bias,
            )
        else:
            self.q_a_proj = get_linear(
                weight=weights.get_weights(f"{prefix}.q_a_proj"),
                bias=(
                    weights.get_tensor(f"{prefix}.q_a_proj.bias")
                    if config.attention_bias
                    else None
                ),
            )
            self.q_a_layernorm = FastRMSNorm.load(
                prefix=f"{prefix}.q_a_layernorm",
                weights=weights,
                eps=config.rms_norm_eps,
            )
            self.q_b_proj = TensorParallelColumnLinear.load(
                config,
                prefix=f"{prefix}.q_b_proj",
                weights=weights,
                bias=config.attention_bias,
            )

        self.kv_a_proj_with_mqa = get_linear(
            weight=weights.get_weights(f"{prefix}.kv_a_proj_with_mqa"),
            bias=(
                weights.get_tensor(f"{prefix}.kv_a_proj_with_mqa.bias")
                if config.attention_bias
                else None
            ),
        )

        self.kv_scales = get_kv_scales(weights, f"{prefix}")

        self.kv_a_layernorm = FastRMSNorm.load(
            prefix=f"{prefix}.kv_a_layernorm", weights=weights, eps=config.rms_norm_eps
        )

        self.kv_b_proj = TensorParallelColumnLinear.load(
            config,
            prefix=f"{prefix}.kv_b_proj",
            weights=weights,
            bias=config.attention_bias,
        )

        self.o_proj = TensorParallelRowLinear.load(
            config,
            prefix=f"{prefix}.o_proj",
            weights=weights,
            bias=False,
        )
        self.num_groups = self.num_heads // self.num_key_value_heads
        self.kv_head_mapping = torch.arange(
            0, self.num_key_value_heads, dtype=torch.int32, device=weights.device
        ).repeat_interleave(self.num_groups)

        kv_b_proj_weight = get_and_maybe_dequant_weights(self.kv_b_proj.linear).T
        kv_b_proj_weight = kv_b_proj_weight.view(
            self.kv_lora_rank,
            self.num_heads,
            self.qk_nope_head_dim + self.value_head_size,
        )
        W_UK, W_UV = kv_b_proj_weight.split(
            [self.qk_nope_head_dim, self.value_head_size], dim=-1
        )
        # Convert from (L, N, V) to (N, L, V)
        self.W_UV = W_UV.transpose(0, 1)
        # Convert from (L, N, P) to (N, P, L)
        self.W_UK_T = W_UK.permute(1, 2, 0)

    def _q_proj_and_k_up_proj(self, x):
        q_proj = self.q_proj if self.q_lora_rank is None else self.q_b_proj
        q_nope, q_pe = (
            q_proj(x)
            .view(-1, self.num_heads, self.head_size)
            .split([self.qk_nope_head_dim, self.qk_rope_head_dim], dim=-1)
        )

        # Convert from (B, N, P) to (N, B, P)
        q_nope = q_nope.transpose(0, 1)
        # Multiply (N, B, P) x (N, P, L) -> (N, B, L)
        ql_nope = torch.bmm(q_nope, self.W_UK_T)
        # Convert from (N, B, L) to (B, N, L)
        return ql_nope.transpose(0, 1), q_pe

    def _v_up_proj_and_o_proj(self, x):
        # Convert from (B, N, L) to (N, B, L)
        x = x.view(-1, self.num_heads, self.kv_lora_rank).transpose(0, 1)
        # Multiply (N, B, L) x (N, L, V) -> (N, B, V)
        x = torch.bmm(x, self.W_UV)
        # Convert from (N, B, V) to (B, N * V)
        x = x.transpose(0, 1).reshape(-1, self.num_heads * self.value_head_size)
        return self.o_proj(x)

    def forward(
        self,
        hidden_states: torch.Tensor,
        cos: torch.Tensor,
        sin: torch.Tensor,
        cu_seqlen_prefill: torch.Tensor,
        kv_cache: KVCache,
        slots: torch.Tensor,
        seqlen: Seqlen,
        hpu_attention_meta: Optional[HPUPagedAttentionMetadata],
    ):
        if self.q_lora_rank is None:
            hidden_states_or_q_c = hidden_states
        else:
            hidden_states_or_q_c = self.q_a_layernorm(self.q_a_proj(hidden_states))[0]

        compressed_kv = self.kv_a_proj_with_mqa(hidden_states)
        compressed_kv, key_pe = torch.split(
            compressed_kv, [self.kv_lora_rank, self.qk_rope_head_dim], dim=-1
        )

        key_pe = key_pe.view(-1, 1, self.qk_rope_head_dim)
        kv_c_normed = self.kv_a_layernorm(compressed_kv.contiguous())[0]

        # Prefill
        if cu_seqlen_prefill is not None:
            q_proj = self.q_proj if self.q_lora_rank is None else self.q_b_proj
            query = q_proj(hidden_states_or_q_c)
            query = query.view(-1, self.num_heads, self.head_size)
            query_nope, query_pe = torch.split(
                query, [self.qk_nope_head_dim, self.qk_rope_head_dim], dim=-1
            )
        else:
            query_nope, query_pe = self._q_proj_and_k_up_proj(hidden_states_or_q_c)

        batch_size, heads, head_dim = query_pe.shape
        query_pe = (
            query_pe.view(batch_size, heads, head_dim // 2, 2)
            .transpose(2, 3)
            .reshape(batch_size, heads, head_dim)
        )
        batch_size, heads, head_dim = key_pe.shape
        key_pe = (
            key_pe.view(batch_size, heads, head_dim // 2, 2)
            .transpose(2, 3)
            .reshape(batch_size, heads, head_dim)
        )
        self.rotary_emb(query_pe, key_pe, cos, sin)
        latent_vec_k = torch.concat(
            (kv_c_normed, key_pe.view(-1, self.qk_rope_head_dim)), dim=-1
        )
        latent_vec_k = latent_vec_k.view(-1, self.qk_rope_head_dim + self.kv_lora_rank)

        latent_vec_k = latent_vec_k.unflatten(0, (slots.size(0), -1))

        kv_cache.store(
            key=latent_vec_k,
            value=None,
            slots=slots,
            kv_scales=self.kv_scales,
        )

        if cu_seqlen_prefill is not None:
            kv = self.kv_b_proj(kv_c_normed).view(
                -1,
                self.num_key_value_heads,
                self.qk_nope_head_dim + self.value_head_size,
            )

            key_nope, value = torch.split(
                kv, [self.qk_nope_head_dim, self.value_head_size], dim=-1
            )
            query[..., self.qk_nope_head_dim :] = query_pe
            key = torch.empty_like(query)
            key[..., : self.qk_nope_head_dim] = key_nope
            key[..., self.qk_nope_head_dim :] = key_pe

            # We need to pad the heads because Flash Attention does not support
            # qk and v with different head sizes.
            query = torch.nn.functional.pad(
                query, (0, self.head_pad_size - self.head_size), value=0
            )
            key = torch.nn.functional.pad(
                key, (0, self.head_pad_size - self.head_size), value=0
            )
            value = torch.nn.functional.pad(
                value, (0, self.head_pad_size - self.value_head_size), value=0
            )

            # flash attention
            attn_output = attention(
                query=query,
                key=key,
                value=value,
                kv_cache=kv_cache,
                kv_scales=self.kv_scales,
                seqlen=seqlen,
                softmax_scale=self.softmax_scale,
            )
            attn_output = attn_output[..., : self.value_head_size]

            return self.o_proj(
                attn_output.reshape(-1, self.num_heads * self.value_head_size)
            )
        else:
            # Decode
            query = torch.cat([query_nope, query_pe], dim=-1)
            attn_output = paged_attention_mla(
                query,
                kv_cache,
                self.kv_head_mapping,
                self.softmax_scale,
                seqlen,
                kv_scales=self.kv_scales,
                hpu_attention_meta=hpu_attention_meta,
                kv_lora_rank=self.kv_lora_rank,
            )
            attn_output = self._v_up_proj_and_o_proj(attn_output)
            return attn_output


class DeepseekV3MLP(nn.Module):
    def __init__(self, prefix: str, config, weights, intermediate_size: int):
        super().__init__()
        self.hidden_act = config.hidden_act
        if self.hidden_act != "silu":
            # Bail out because MoE only supports silu.
            raise NotImplementedError(
                "Currently only `silu` is supported as an activation for Deepseek V2."
            )
        self.act = ACT2FN[self.hidden_act]

        self.gate_up_proj = TensorParallelColumnLinear.load_multi(
            config,
            prefixes=[f"{prefix}.gate_proj", f"{prefix}.up_proj"],
            weights=weights,
            dim=0,
            bias=False,
        )

        self.down_proj = TensorParallelRowLinear.load(
            config,
            prefix=f"{prefix}.down_proj",
            weights=weights,
            bias=False,
        )

        self.intermediate_size = intermediate_size // weights.process_group.size()

        # TODO: This is a hotfix to be removed & properly refactored.
        self.quantize = config.quantize

    def forward(self, hidden_states: torch.Tensor, reduce: bool = True):
        gate_up_states = self.gate_up_proj(hidden_states)
        gate_up_states = gate_up_states.view(-1, 2, self.intermediate_size)
        return self.down_proj(
            self.act(gate_up_states[:, 0]) * gate_up_states[:, 1], reduce=reduce
        )


class DeepseekV3MoE(nn.Module):
    def __init__(
        self,
        prefix,
        config: DeepseekV3Config,
        moe_layer_cls: Type[MoELayer],
        weights,
    ):
        super().__init__()

        self.hidden_dim = config.hidden_size
        self.moe_intermediate_size = (
            config.moe_intermediate_size // weights.process_group.size()
        )
        self.routed_scaling_factor = config.routed_scaling_factor

        # Gating
        self.gate = FastLinear.load(config, f"{prefix}.gate", weights, bias=False)

        if config.topk_method == "noaux_tc":
            self.gate.e_score_correction_bias = torch.zeros(
                config.n_routed_experts, device=weights.device
            )
        else:
            self.gate.e_score_correction_bias = None

        self.moe_layer = moe_layer_cls(
            prefix=f"{prefix}.experts",
            n_experts=config.n_routed_experts,
            n_expert_group=config.n_group,
            renormalize=config.norm_topk_prob,
            topk=config.num_experts_per_tok,
            topk_group=config.topk_group,
            weights=weights,
            scoring_func=config.scoring_func,
            e_score_correction_bias=self.gate.e_score_correction_bias,
        )
        assert isinstance(self.moe_layer, MoELayer)

        if config.n_shared_experts is not None:
            self.shared_experts = DeepseekV3MLP(
                prefix=f"{prefix}.shared_experts",
                config=config,
                weights=weights,
                intermediate_size=config.moe_intermediate_size
                * config.n_shared_experts,
            )
        else:
            self.shared_experts = None

        self.process_group = weights.process_group

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if self.shared_experts is not None:
            shared_output = self.shared_experts(x, reduce=False)
        else:
            shared_output = None

        router_logits = self.gate(x)

        out = self.moe_layer(x, gating_output=router_logits)

        if shared_output is not None:
            out = out + shared_output

        # Reduce sum
        if self.process_group.size() > 1:
            torch.distributed.all_reduce(out, group=self.process_group)

        return out.view(*x.shape)


class DeepseekV3Layer(nn.Module):
    def __init__(self, prefix, layer_id, config, weights, rotary_emb):
        super().__init__()
        prefix = f"{prefix}.layers.{layer_id}"

        self.self_attn = DeepseekV3Attention(
            prefix=f"{prefix}.self_attn",
            config=config,
            weights=weights,
            rotary_emb=rotary_emb,
        )

        if (
            config.n_routed_experts is not None
            and layer_id >= config.first_k_dense_replace
            and layer_id % config.moe_layer_freq == 0
        ):
            moe_layer_cls = (
                SparseMoELayer
                if SparseMoELayer.is_supported(weights)
                else DenseMoELayer
            )
            self.mlp = DeepseekV3MoE(f"{prefix}.mlp", config, moe_layer_cls, weights)
        else:
            self.mlp = DeepseekV3MLP(
                prefix=f"{prefix}.mlp",
                config=config,
                weights=weights,
                intermediate_size=config.intermediate_size,
            )

        self.input_layernorm = FastRMSNorm.load(
            prefix=f"{prefix}.input_layernorm", weights=weights, eps=config.rms_norm_eps
        )
        self.post_attention_layernorm = FastRMSNorm.load(
            prefix=f"{prefix}.post_attention_layernorm",
            weights=weights,
            eps=config.rms_norm_eps,
        )

    def forward(
        self,
        hidden_states: torch.Tensor,
        residual: torch.Tensor,
        cos: torch.Tensor,
        sin: torch.Tensor,
        cu_seqlen_prefill: torch.Tensor,
        kv_cache,
        slots: torch.Tensor,
        seqlen: Seqlen,
        hpu_attention_meta: Optional[HPUPagedAttentionMetadata],
    ):
        normed_hidden_states, residual = self.input_layernorm(hidden_states, residual)

        # Self Attention
        attn_output = self.self_attn(
            normed_hidden_states,
            cos,
            sin,
            cu_seqlen_prefill,
            kv_cache,
            slots,
            seqlen,
            hpu_attention_meta,
        )

        # faster post attention rms norm
        normed_attn_res_output, residual = self.post_attention_layernorm(
            attn_output, residual
        )

        output = self.mlp(normed_attn_res_output)

        return output, residual


class DeepseekV3Model(torch.nn.Module):
    def __init__(self, prefix: str, config, weights: Weights):
        super().__init__()

        self.embed_tokens = TensorParallelEmbedding(
            prefix=f"{prefix}.embed_tokens", weights=weights
        )
        rotary_emb = PositionRotaryEmbedding.static(
            config=config,
            dim=config.qk_rope_head_dim,
            base=config.rope_theta,
            device=weights.device,
        )

        self.layers = nn.ModuleList(
            [
                DeepseekV3Layer(
                    prefix,
                    layer_id,
                    config,
                    weights,
                    rotary_emb,
                )
                for layer_id in range(config.num_hidden_layers)
            ]
        )
        self.norm = FastRMSNorm.load(
            prefix=f"{prefix}.norm", weights=weights, eps=config.rms_norm_eps
        )

        self.head_size = self.layers[0].self_attn.head_size
        self.num_heads = self.layers[0].self_attn.num_heads
        self.num_key_value_heads = self.layers[0].self_attn.num_key_value_heads

    def forward(
        self,
        input_ids: torch.Tensor,
        position_ids: torch.Tensor,
        cu_seqlen_prefill: Optional[torch.Tensor],
        kv_cache: List[Tuple[torch.Tensor, torch.Tensor]],
        slots: torch.Tensor,
        seqlen: Seqlen,
        hpu_attention_meta: Optional[HPUPagedAttentionMetadata],
    ) -> torch.Tensor:
        if hpu_attention_meta is not None:
            hpu_attention_meta = set_block_mapping(
                hpu_attention_meta, input_ids.shape[0]
            )
        hidden_states = self.embed_tokens(input_ids)

        # Get rotary cos and sin for this forward
        # Avoid to index in each layer
        cos, sin = self.layers[0].self_attn.rotary_emb.get_cos_sin(position_ids)

        residual = None
        lazy_mode = htorch.utils.internal.is_lazy()
        if lazy_mode:
            htorch.core.mark_step()
        for i, layer in enumerate(self.layers):
            hidden_states, residual = layer(
                hidden_states,
                residual,
                cos,
                sin,
                cu_seqlen_prefill,
                kv_cache[i],
                slots,
                seqlen,
                hpu_attention_meta,
            )
            if lazy_mode:
                htorch.core.mark_step()

        hidden_states, _ = self.norm(hidden_states, residual)

        return hidden_states


class FlashDeepseekV3ForCausalLM(torch.nn.Module):
    def __init__(self, prefix: str, config, weights: Weights):
        super().__init__()

        self.model = DeepseekV3Model(
            "model" if not prefix else f"{prefix}.model", config, weights
        )
        self.lm_head = SpeculativeHead.load(
            config,
            prefix="lm_head" if not prefix else f"{prefix}.lm_head",
            weights=weights,
        )

    def forward(
        self,
        input_ids: torch.Tensor,
        position_ids: torch.Tensor,
        cu_seqlen_prefill: Optional[torch.Tensor],
        kv_cache: List[Tuple[torch.Tensor, torch.Tensor]],
        slots: torch.Tensor,
        seqlen: Seqlen,
        hpu_attention_meta: Optional[HPUPagedAttentionMetadata],
        lm_head_indices: Optional[torch.Tensor] = None,
        adapter_data: Optional[torch.Tensor] = None,
    ) -> Tuple[torch.Tensor, Optional[torch.Tensor]]:
        hidden_states = self.model(
            input_ids,
            position_ids,
            cu_seqlen_prefill,
            kv_cache,
            slots,
            seqlen,
            hpu_attention_meta,
        )
        if lm_head_indices is not None:
            hidden_states = hidden_states[lm_head_indices]
        logits, speculative_logits = self.lm_head(hidden_states)
        return logits, speculative_logits
