import math
import numpy as np
import torch
import torch.nn as nn
from torch.cuda.amp import custom_bwd, custom_fwd
import gc
from torch.utils.cpp_extension import load
import os


path = os.path.dirname(os.path.dirname(__file__))

gptq_extension = load(
    name='gptq_extension',
    sources=[os.path.join(path,'gptq/q_gemm.cu')],
    verbose=True
)
try:
    import triton
    import triton.language as tl
    from . import custom_autotune

    # code based https://github.com/fpgaminer/GPTQ-triton
    @custom_autotune.autotune(
        configs=[
            triton.Config({
                'BLOCK_SIZE_M': 64,
                'BLOCK_SIZE_N': 256,
                'BLOCK_SIZE_K': 32,
                'GROUP_SIZE_M': 8
            }, num_stages=4, num_warps=4),
            triton.Config({
                'BLOCK_SIZE_M': 128,
                'BLOCK_SIZE_N': 128,
                'BLOCK_SIZE_K': 32,
                'GROUP_SIZE_M': 8
            }, num_stages=4, num_warps=4),
            triton.Config({
                'BLOCK_SIZE_M': 64,
                'BLOCK_SIZE_N': 128,
                'BLOCK_SIZE_K': 32,
                'GROUP_SIZE_M': 8
            }, num_stages=4, num_warps=4),
            triton.Config({
                'BLOCK_SIZE_M': 128,
                'BLOCK_SIZE_N': 32,
                'BLOCK_SIZE_K': 32,
                'GROUP_SIZE_M': 8
            }, num_stages=4, num_warps=4),
            triton.Config({
                'BLOCK_SIZE_M': 64,
                'BLOCK_SIZE_N': 64,
                'BLOCK_SIZE_K': 32,
                'GROUP_SIZE_M': 8
            }, num_stages=4, num_warps=4),
            triton.Config({
                'BLOCK_SIZE_M': 64,
                'BLOCK_SIZE_N': 128,
                'BLOCK_SIZE_K': 32,
                'GROUP_SIZE_M': 8
            }, num_stages=2, num_warps=8),
            triton.Config({
                'BLOCK_SIZE_M': 64,
                'BLOCK_SIZE_N': 64,
                'BLOCK_SIZE_K': 64,
                'GROUP_SIZE_M': 8
            }, num_stages=3, num_warps=8),
            triton.Config({
                'BLOCK_SIZE_M': 32,
                'BLOCK_SIZE_N': 32,
                'BLOCK_SIZE_K': 128,
                'GROUP_SIZE_M': 8
            }, num_stages=2, num_warps=4),
        ],
        key=['M', 'N', 'K'],
        nearest_power_of_two=True,
        prune_configs_by={
            'early_config_prune': custom_autotune.matmul248_kernel_config_pruner,
            'perf_model': None,
            'top_k': None,
        },
    )
    @triton.jit
    def matmul_248_kernel(a_ptr, b_ptr, c_ptr, scales_ptr, zeros_ptr, g_ptr ,M, N, K, bits, maxq, stride_am, stride_ak, stride_bk, stride_bn, stride_cm, stride_cn, stride_scales, stride_zeros,
                          BLOCK_SIZE_M: tl.constexpr, BLOCK_SIZE_N: tl.constexpr, BLOCK_SIZE_K: tl.constexpr, GROUP_SIZE_M: tl.constexpr):
        """
        Compute the matrix multiplication C = A x B.
        A is of shape (M, K) float16
        B is of shape (K//8, N) int32
        C is of shape (M, N) float16
        scales is of shape (G, N) float16
        zeros is of shape (G, N) float16
        g_ptr is of shape (K) int32 
        """
        infearure_per_bits = 32 // bits

        pid = tl.program_id(axis=0)
        num_pid_m = tl.cdiv(M, BLOCK_SIZE_M)
        num_pid_n = tl.cdiv(N, BLOCK_SIZE_N)
        num_pid_k = tl.cdiv(K, BLOCK_SIZE_K)
        num_pid_in_group = GROUP_SIZE_M * num_pid_n
        group_id = pid // num_pid_in_group
        first_pid_m = group_id * GROUP_SIZE_M
        group_size_m = min(num_pid_m - first_pid_m, GROUP_SIZE_M)
        pid_m = first_pid_m + (pid % group_size_m)
        pid_n = (pid % num_pid_in_group) // group_size_m

        offs_am = pid_m * BLOCK_SIZE_M + tl.arange(0, BLOCK_SIZE_M)
        offs_bn = pid_n * BLOCK_SIZE_N + tl.arange(0, BLOCK_SIZE_N)
        offs_k = tl.arange(0, BLOCK_SIZE_K)
        a_ptrs = a_ptr + (offs_am[:, None] * stride_am + offs_k[None, :] * stride_ak)  # (BLOCK_SIZE_M, BLOCK_SIZE_K)
        a_mask = (offs_am[:, None] < M)
        # b_ptrs is set up such that it repeats elements along the K axis 8 times
        b_ptrs = b_ptr + ((offs_k[:, None] // infearure_per_bits) * stride_bk + offs_bn[None, :] * stride_bn)  # (BLOCK_SIZE_K, BLOCK_SIZE_N)
        g_ptrs = g_ptr + offs_k
        # shifter is used to extract the N bits of each element in the 32-bit word from B
        scales_ptrs = scales_ptr + offs_bn[None, :]
        zeros_ptrs = zeros_ptr + (offs_bn[None, :] // infearure_per_bits)

        shifter = (offs_k % infearure_per_bits) * bits
        zeros_shifter = (offs_bn % infearure_per_bits) * bits
        accumulator = tl.zeros((BLOCK_SIZE_M, BLOCK_SIZE_N), dtype=tl.float32)

        
        # a_full_ptrs = a_ptr + (offs_am[:, None] * stride_am + offs_k[None, :] * stride_ak)
        # a_saved_full_ptrs = a_saved_ptr + (offs_am[:, None] * stride_am + offs_k[None, :] * stride_ak)
        # a_full = tl.load(a_full_ptrs, mask=a_mask, other=0.0)
        # tl.store(a_saved_full_ptrs, a_full, mask=a_mask)
                
        for k in range(0, num_pid_k):
            g_idx = tl.load(g_ptrs)

            # Fetch scales and zeros; these are per-outfeature and thus reused in the inner loop
            scales = tl.load(scales_ptrs + g_idx[:, None] * stride_scales)  # (BLOCK_SIZE_K, BLOCK_SIZE_N,)
            zeros = tl.load(zeros_ptrs + g_idx[:, None] * stride_zeros) # (BLOCK_SIZE_K, BLOCK_SIZE_N,)

            zeros = (zeros >> zeros_shifter[None, :]) & maxq
            zeros = (zeros + 1)

            a = tl.load(a_ptrs, mask=a_mask, other=0.)  # (BLOCK_SIZE_M, BLOCK_SIZE_K)
            b = tl.load(b_ptrs)  # (BLOCK_SIZE_K, BLOCK_SIZE_N), but repeated
            
            # a_saved_ptrs = a_saved_ptr + (offs_am[:, None] * stride_am + offs_k[None, :] * stride_ak)
            # tl.store(a_saved_ptrs, a, mask=a_mask)            

            # Now we need to unpack b (which is N-bit values) into 32-bit values
            b = (b >> shifter[:, None]) & maxq  # Extract the N-bit values
            b = (b - zeros) * scales  # Scale and shift
            
            # b_saved_ptrs = b_saved_ptr + ((offs_k[:, None] // infearure_per_bits) * stride_bk + offs_bn[None, :] * stride_bn)
            # tl.store(b_saved_ptrs, b) 

            accumulator += tl.dot(a, b.to(a.dtype))
            a_ptrs += BLOCK_SIZE_K
            b_ptrs += (BLOCK_SIZE_K // infearure_per_bits) * stride_bk
            g_ptrs += BLOCK_SIZE_K

        c_ptrs = c_ptr + stride_cm * offs_am[:, None] + stride_cn * offs_bn[None, :]
        c_mask = (offs_am[:, None] < M) & (offs_bn[None, :] < N)
        tl.store(c_ptrs, accumulator, mask=c_mask)
        
        # if pid == 0:  # Only need to store shape once
        #     tl.store(b_shape_ptr + 0, K)  # Number of rows
        #     tl.store(b_shape_ptr + 1, N)  # Number of columns

    @custom_autotune.autotune(configs=[
        triton.Config({
            'BLOCK_SIZE_M': 64,
            'BLOCK_SIZE_N': 32,
            'BLOCK_SIZE_K': 256,
            'GROUP_SIZE_M': 8
        }, num_stages=4, num_warps=4),
        triton.Config({
            'BLOCK_SIZE_M': 128,
            'BLOCK_SIZE_N': 32,
            'BLOCK_SIZE_K': 128,
            'GROUP_SIZE_M': 8
        }, num_stages=4, num_warps=4),
        triton.Config({
            'BLOCK_SIZE_M': 64,
            'BLOCK_SIZE_N': 32,
            'BLOCK_SIZE_K': 128,
            'GROUP_SIZE_M': 8
        }, num_stages=4, num_warps=4),
        triton.Config({
            'BLOCK_SIZE_M': 128,
            'BLOCK_SIZE_N': 32,
            'BLOCK_SIZE_K': 32,
            'GROUP_SIZE_M': 8
        }, num_stages=4, num_warps=4),
        triton.Config({
            'BLOCK_SIZE_M': 64,
            'BLOCK_SIZE_N': 32,
            'BLOCK_SIZE_K': 64,
            'GROUP_SIZE_M': 8
        }, num_stages=4, num_warps=4),
        triton.Config({
            'BLOCK_SIZE_M': 64,
            'BLOCK_SIZE_N': 32,
            'BLOCK_SIZE_K': 128,
            'GROUP_SIZE_M': 8
        }, num_stages=2, num_warps=8),
        triton.Config({
            'BLOCK_SIZE_M': 64,
            'BLOCK_SIZE_N': 64,
            'BLOCK_SIZE_K': 64,
            'GROUP_SIZE_M': 8
        }, num_stages=3, num_warps=8),
        triton.Config({
            'BLOCK_SIZE_M': 32,
            'BLOCK_SIZE_N': 128,
            'BLOCK_SIZE_K': 32,
            'GROUP_SIZE_M': 8
        }, num_stages=2, num_warps=4),
    ],
                              key=['M', 'N', 'K'],
                              nearest_power_of_two=True)
    @triton.jit
    def transpose_matmul_248_kernel(a_ptr, b_ptr, c_ptr, scales_ptr, zeros_ptr, g_ptr, M, N, K, bits, maxq, stride_am, stride_ak, stride_bk, stride_bn, stride_cm, stride_cn, stride_scales,
                                    stride_zeros, BLOCK_SIZE_M: tl.constexpr, BLOCK_SIZE_N: tl.constexpr, BLOCK_SIZE_K: tl.constexpr, GROUP_SIZE_M: tl.constexpr):
        """
        Compute the matrix multiplication C = A x B.
        A is of shape (M, N) float16
        B is of shape (K//8, N) int32
        C is of shape (M, K) float16
        scales is of shape (G, N) float16
        zeros is of shape (G, N) float16
        g_ptr is of shape (K) int32 
        """
        infearure_per_bits = 32 // bits

        pid = tl.program_id(axis=0)
        num_pid_m = tl.cdiv(M, BLOCK_SIZE_M)
        num_pid_k = tl.cdiv(K, BLOCK_SIZE_K)
        num_pid_n = tl.cdiv(N, BLOCK_SIZE_N)
        num_pid_in_group = GROUP_SIZE_M * num_pid_k
        group_id = pid // num_pid_in_group
        first_pid_m = group_id * GROUP_SIZE_M
        group_size_m = min(num_pid_m - first_pid_m, GROUP_SIZE_M)
        pid_m = first_pid_m + (pid % group_size_m)
        pid_k = (pid % num_pid_in_group) // group_size_m

        offs_am = pid_m * BLOCK_SIZE_M + tl.arange(0, BLOCK_SIZE_M)
        offs_bk = pid_k * BLOCK_SIZE_K + tl.arange(0, BLOCK_SIZE_K)
        offs_n = tl.arange(0, BLOCK_SIZE_N)
        a_ptrs = a_ptr + (offs_am[:, None] * stride_am + offs_n[None, :] * stride_ak)  # (BLOCK_SIZE_M, BLOCK_SIZE_N)
        a_mask = (offs_am[:, None] < M)
        # b_ptrs is set up such that it repeats elements along the K axis 8 times
        b_ptrs = b_ptr + ((offs_bk[:, None] // infearure_per_bits) * stride_bk + offs_n[None, :] * stride_bn)  # (BLOCK_SIZE_K, BLOCK_SIZE_N)
        g_ptrs = g_ptr + offs_bk
        g_idx = tl.load(g_ptrs)

        # shifter is used to extract the N bits of each element in the 32-bit word from B
        scales_ptrs = scales_ptr + offs_n[None, :] + g_idx[:, None] * stride_scales
        zeros_ptrs = zeros_ptr + (offs_n[None, :] // infearure_per_bits) + g_idx[:, None] * stride_zeros

        shifter = (offs_bk % infearure_per_bits) * bits
        zeros_shifter = (offs_n % infearure_per_bits) * bits
        accumulator = tl.zeros((BLOCK_SIZE_M, BLOCK_SIZE_K), dtype=tl.float32)

        for n in range(0, num_pid_n):
            # Fetch scales and zeros; these are per-outfeature and thus reused in the inner loop
            scales = tl.load(scales_ptrs) # (BLOCK_SIZE_K, BLOCK_SIZE_N,)
            zeros = tl.load(zeros_ptrs)  # (BLOCK_SIZE_K, BLOCK_SIZE_N,)

            zeros = (zeros >> zeros_shifter[None, :]) & maxq
            zeros = (zeros + 1)

            a = tl.load(a_ptrs, mask=a_mask, other=0.)  # (BLOCK_SIZE_M, BLOCK_SIZE_N)
            b = tl.load(b_ptrs)  # (BLOCK_SIZE_K, BLOCK_SIZE_N), but repeated

            # Now we need to unpack b (which is N-bit values) into 32-bit values
            b = (b >> shifter[:, None]) & maxq  # Extract the N-bit values
            b = (b - zeros) * scales  # Scale and shift
            b = tl.trans(b)

            accumulator += tl.dot(a, b)
            a_ptrs += BLOCK_SIZE_N
            b_ptrs += BLOCK_SIZE_N
            scales_ptrs += BLOCK_SIZE_N
            zeros_ptrs += (BLOCK_SIZE_N // infearure_per_bits)

        c_ptrs = c_ptr + stride_cm * offs_am[:, None] + stride_cn * offs_bk[None, :]
        c_mask = (offs_am[:, None] < M) & (offs_bk[None, :] < K)
        tl.store(c_ptrs, accumulator, mask=c_mask)
except:
    print('triton not installed.')


def matmul248(input, qweight, scales, qzeros, g_idx, bits, maxq):
    with torch.cuda.device(input.device):
        output = torch.empty((input.shape[0], qweight.shape[1]), device=input.device, dtype=torch.bfloat16)
        grid = lambda META: (triton.cdiv(input.shape[0], META['BLOCK_SIZE_M']) * triton.cdiv(qweight.shape[1], META['BLOCK_SIZE_N']), )
        matmul_248_kernel[grid](input, qweight, output, scales, qzeros, g_idx,input.shape[0], qweight.shape[1], input.shape[1], bits, maxq, input.stride(0), input.stride(1), qweight.stride(0),
                                qweight.stride(1), output.stride(0), output.stride(1), scales.stride(0), qzeros.stride(0))
        # import pdb; pdb.set_trace()
        return output


def transpose_matmul248(input, qweight, scales, qzeros, g_idx, bits, maxq):
    with torch.cuda.device(input.device):
        output_dim = (qweight.shape[0] * 32) // bits
        output = torch.empty((input.shape[0], output_dim), device=input.device, dtype=torch.float16)
        grid = lambda META: (triton.cdiv(input.shape[0], META['BLOCK_SIZE_M']) * triton.cdiv(output_dim, META['BLOCK_SIZE_K']), )
        transpose_matmul_248_kernel[grid](input, qweight, output, scales, qzeros, g_idx, input.shape[0], qweight.shape[1], output_dim, bits, maxq, input.stride(0), input.stride(1), qweight.stride(0),
                                          qweight.stride(1), output.stride(0), output.stride(1), scales.stride(0), qzeros.stride(0))
        return output


class QuantLinearFunction(torch.autograd.Function):

    @staticmethod
    @custom_fwd(cast_inputs=torch.bfloat16)
    def forward(ctx, input, qweight, scales, qzeros, g_idx, bits, maxq):
        output = matmul248(input, qweight, scales, qzeros, g_idx, bits, maxq)
        ctx.save_for_backward(qweight, scales, qzeros, g_idx)
        ctx.bits, ctx.maxq = bits, maxq
        return output

    @staticmethod
    @custom_bwd
    def backward(ctx, grad_output):
        qweight, scales, qzeros, g_idx = ctx.saved_tensors
        bits, maxq = ctx.bits, ctx.maxq
        grad_input = None

        if ctx.needs_input_grad[0]:
            grad_input = transpose_matmul248(grad_output, qweight, scales, qzeros, g_idx, bits, maxq)
        return grad_input, None, None, None, None, None, None


class QuantLinear(nn.Module):

    def __init__(self, bits, groupsize, infeatures, outfeatures, bias):
        super().__init__()
        if bits not in [2, 4, 8]:
            raise NotImplementedError("Only 2,4,8 bits are supported.")
        self.infeatures = infeatures
        self.outfeatures = outfeatures
        self.bits = bits
        self.maxq = 2**self.bits - 1
        self.groupsize = groupsize if groupsize != -1 else infeatures

        self.register_buffer('qweight', torch.zeros((infeatures // 32 * self.bits, outfeatures), dtype=torch.int32))
        self.register_buffer('qzeros', torch.zeros((math.ceil(infeatures / self.groupsize), outfeatures // 32 * self.bits), dtype=torch.int32))
        self.register_buffer('scales', torch.zeros((math.ceil(infeatures / self.groupsize), outfeatures), dtype=torch.float16))
        self.register_buffer('g_idx', torch.tensor([i // self.groupsize for i in range(infeatures)], dtype=torch.int32))
        if bias:
            self.register_buffer('bias', torch.zeros((outfeatures), dtype=torch.float16))
        else:
            self.bias = None

    def pack(self, linear, scales, zeros, g_idx=None):
        self.g_idx = g_idx.clone() if g_idx is not None else self.g_idx

        scales = scales.t().contiguous()
        zeros = zeros.t().contiguous()
        scale_zeros = zeros * scales
        self.scales = scales.clone().half()
        if linear.bias is not None:
            self.bias = linear.bias.clone().half()

        intweight = []
        for idx in range(self.infeatures):
            intweight.append(torch.round((linear.weight.data[:, idx] + scale_zeros[self.g_idx[idx]]) / self.scales[self.g_idx[idx]]).to(torch.int)[:, None])
        intweight = torch.cat(intweight, dim=1)
        intweight = intweight.t().contiguous()
        intweight = intweight.numpy().astype(np.uint32)
        qweight = np.zeros((intweight.shape[0] // 32 * self.bits, intweight.shape[1]), dtype=np.uint32)
        i = 0
        row = 0
        while row < qweight.shape[0]:
            if self.bits in [2, 4, 8]:
                for j in range(i, i + (32 // self.bits)):
                    qweight[row] |= intweight[j] << (self.bits * (j - i))
                i += 32 // self.bits
                row += 1
            else:
                raise NotImplementedError("Only 2,4,8 bits are supported.")

        qweight = qweight.astype(np.int32)
        self.qweight = torch.from_numpy(qweight)

        zeros -= 1
        zeros = zeros.numpy().astype(np.uint32)
        qzeros = np.zeros((zeros.shape[0], zeros.shape[1] // 32 * self.bits), dtype=np.uint32)
        i = 0
        col = 0
        while col < qzeros.shape[1]:
            if self.bits in [2, 4, 8]:
                for j in range(i, i + (32 // self.bits)):
                    qzeros[:, col] |= zeros[:, j] << (self.bits * (j - i))
                i += 32 // self.bits
                col += 1
            else:
                raise NotImplementedError("Only 2,4,8 bits are supported.")

        qzeros = qzeros.astype(np.int32)
        self.qzeros = torch.from_numpy(qzeros)

    def forward(self, x):
        out_shape = x.shape[:-1] + (self.outfeatures, )
        out = QuantLinearFunction.apply(x.reshape(-1, x.shape[-1]), self.qweight, self.scales, self.qzeros, self.g_idx, self.bits, self.maxq)
        out = out + self.bias if self.bias is not None else out
        return out.reshape(out_shape)


class Dequantizer:
    def __init__(self, bit, scales, g_idx, qweight, qzeros):
        self.bit = bit
        self.scales = scales
        self.g_idx = g_idx
        self.qweight = qweight
        self.qzeros = qzeros
        # self.scale_zeros = scale_zeros

    def dequant(self):
        bit = self.bit

        # Convert qweight and qzeros to numpy arrays if they are not already
        if isinstance(self.qweight, torch.Tensor):
            qweight = self.qweight.numpy()
        else:
            qweight = self.qweight

        if isinstance(self.qzeros, torch.Tensor):
            qzeros = self.qzeros.numpy()
        else:
            qzeros = self.qzeros

        # Dequantize qweight
        intweight = np.zeros((qweight.shape[0] * 32 // bit, qweight.shape[1]), dtype=np.int32)
        i = 0
        row = 0
        while row < qweight.shape[0]:
            if bit in [2, 4, 8]:
                for j in range(i, i + (32 // bit)):
                    intweight[j] = (qweight[row] >> (bit * (j - i))) & ((1 << bit) - 1)
                i += 32 // bit
                row += 1
            else:
                raise NotImplementedError("Only 2, 4, 8 bits are supported.")
        intweight = intweight.astype(np.float32)

        # Dequantize qzeros
        zeros = np.zeros((qzeros.shape[0], qzeros.shape[1] * 32 // bit), dtype=np.int32)
        i = 0
        col = 0
        while col < qzeros.shape[1]:
            if bit in [2, 4, 8]:
                for j in range(i, i + (32 // bit)):
                    zeros[:, j] = (qzeros[:, col] >> (bit * (j - i))) & ((1 << bit) - 1)
                i += 32 // bit
                col += 1
            else:
                raise NotImplementedError("Only 2, 4, 8 bits are supported.")
        zeros = zeros.astype(np.float32)
        zeros += 1
        scale_zeros = (torch.tensor(zeros) * self.scales).to(torch.bfloat16)
        # import pdb; pdb.set_trace()
        # Convert intweight and zeros to Tensor
        intweight = torch.from_numpy(intweight).to(torch.bfloat16)
        zeros = torch.from_numpy(zeros).to(torch.bfloat16)
        # import pdb; pdb.set_trace() 
        # Reshape intweight to match the original fp16_weight shape
        fp16_weight_shape = (intweight.shape[1],len(self.g_idx))  
        dequantized_weight = torch.zeros(fp16_weight_shape, dtype=torch.bfloat16)
        for idx in range(fp16_weight_shape[1]):
            dequantized_weight[:, idx] = (intweight[idx, :] * self.scales[self.g_idx[idx]] - scale_zeros[self.g_idx[idx]]).to(torch.bfloat16) 

        return dequantized_weight

class MixquantLinear(nn.Module):
    def __init__(self, bits, groupsize, input_size, output_size,S,index_dict,name):
        super().__init__()
        self.input_size = input_size
        self.output_size = output_size
        self.bits = bits
        self.maxq = 2**self.bits[0] - 1
        self.groupsize = groupsize
        
        for bit in bits:
            st = index_dict[f"self_attn_{bit}"][0] if "self_attn" in name else index_dict[f"mlp_{bit}"][0]
            ed = index_dict[f"self_attn_{bit}"][-1] if "self_attn" in name else index_dict[f"mlp_{bit}"][-1]
            if (ed - st) % 32 != 0: # to make sure the length of the layer is divisible by 32
                t = ed - st
                t = t - (t % 32)
                ed = st + t
            self.register_buffer(f'S_{bit}', S[st:ed])
            # self.register_buffer("U",torch.empty((ed-st,output_size)))
            # self.register_buffer("V",torch.empty((input_size,ed-st)))
        
        for quant_type in ['U','V']:
            for bit in bits: # operate as V.T
                intermediate_size = getattr(self,f'S_{bit}').shape[0]
                
                self.register_buffer(f'qweight_{quant_type}_{bit}', torch.zeros((input_size // 32 * bit, intermediate_size), dtype=torch.int32))
                self.register_buffer(f'qzeros_{quant_type}_{bit}', torch.zeros((math.ceil(input_size / groupsize), intermediate_size // 32 * bit), dtype=torch.int32))
                self.register_buffer(f'scales_{quant_type}_{bit}', torch.zeros((math.ceil(input_size / groupsize), intermediate_size), dtype=torch.float16))
                self.register_buffer(f'g_idx_{quant_type}_{bit}', torch.tensor([i // groupsize for i in range(input_size)], dtype=torch.int32))

                if quant_type == 'U': 
                    self.register_buffer(f'qweight_{quant_type}_{bit}', torch.zeros((intermediate_size // 32 * bit, output_size), dtype=torch.int32))
                    self.register_buffer(f'qzeros_{quant_type}_{bit}', torch.zeros((math.ceil(intermediate_size / groupsize), output_size // 32 * bit), dtype=torch.int32))
                    self.register_buffer(f'scales_{quant_type}_{bit}', torch.zeros((math.ceil(intermediate_size / groupsize), output_size), dtype=torch.float16))
                    self.register_buffer(f'g_idx_{quant_type}_{bit}', torch.tensor([i // groupsize for i in range(intermediate_size)], dtype=torch.int32))              
             
    def pack(self,quantizers,name,layers,index_dict):
        for k,v in quantizers.items():
            # import pdb; pdb.set_trace()
            if name in k:
                linear = layers[name]
                
                bit = k.rsplit('.', 1)[1]
                quant_type = k.rsplit('.', 1)[0].rsplit('.', 1)[1]
                
                fp16_weight = getattr(linear,quant_type)
                if "V" in quant_type:
                    fp16_weight = fp16_weight.t()
                
                quantizer, scales, zeros, g_idx, _, _ = v
                # import pdb; pdb.set_trace()
                
                self.g_idx = g_idx.clone() if g_idx is not None else self.g_idx
                setattr(self,f'g_idx_{quant_type}_{bit}',self.g_idx)

                scales = scales.t().contiguous()
                zeros = zeros.t().contiguous()
                scale_zeros = zeros * scales
                
                self.scales = scales.clone().half()
                setattr(self,f'scales_{quant_type}_{bit}',self.scales)
                # if linear.bias is not None:
                #     self.bias = linear.bias.clone().half()
                intweight = []
                st = index_dict[f"self_attn_{bit}"][0] if "self_attn" in name else index_dict[f"mlp_{bit}"][0]
                ed = index_dict[f"self_attn_{bit}"][-1] if "self_attn" in name else index_dict[f"mlp_{bit}"][-1]
                if "U" in quant_type:
                    for idx in range(ed - st):
                        intweight.append(torch.round((fp16_weight.data[:, idx] + scale_zeros[self.g_idx[idx]]) / self.scales[self.g_idx[idx]]).to(torch.int)[:, None])
                else:    
                    for idx in range(fp16_weight.shape[1]):
                        # To Do: Check fp16 weight index
                        intweight.append(torch.round((fp16_weight.data[:ed - st, idx] + scale_zeros[self.g_idx[idx]]) / self.scales[self.g_idx[idx]]).to(torch.int)[:, None])
                # import pdb; pdb.set_trace()
                bit = int(bit)
                intweight = torch.cat(intweight, dim=1).contiguous()
                intweight = intweight.t().contiguous()
                intweight = intweight.numpy().astype(np.uint32)
                qweight = np.zeros((intweight.shape[0] // 32 * bit, intweight.shape[1]), dtype=np.uint32)
                i = 0
                row = 0
                while row < qweight.shape[0]:
                    if bit in [2, 4, 8]:
                        for j in range(i, i + (32 // bit)):
                            qweight[row] |= intweight[j] << (bit * (j - i))
                        i += 32 // bit
                        row += 1
                    else:
                        raise NotImplementedError("Only 2,4,8 bits are supported.")

                qweight = qweight.astype(np.int32)
                self.qweight = torch.from_numpy(qweight)
                setattr(self,f'qweight_{quant_type}_{bit}',self.qweight)
                                
                zeros -= 1
                zeros = zeros.numpy().astype(np.uint32)
                qzeros = np.zeros((zeros.shape[0], zeros.shape[1] // 32 * bit), dtype=np.uint32)
                i = 0
                col = 0
                while col < qzeros.shape[1]:
                    if bit in [2, 4, 8]:
                        for j in range(i, i + (32 // bit)):
                            qzeros[:, col] |= zeros[:, j] << (bit * (j - i))
                        i += 32 // bit
                        col += 1
                    else:
                        raise NotImplementedError("Only 2,4,8 bits are supported.")
                
                qzeros = qzeros.astype(np.int32)
                self.qzeros = torch.from_numpy(qzeros)
                setattr(self,f'qzeros_{quant_type}_{bit}',self.qzeros)
                
                # if quant_type == 'U':
                    
                #     Dequant = Dequantizer(bit, self.scales, self.g_idx, self.qweight, self.qzeros)
                #     weight = Dequant.dequant()
                #     import pdb; pdb.set_trace()
    
    def dequant(self,bit,scales,g_idx,qweight,qzeros):

        # Convert qweight and qzeros to numpy arrays if they are not already
        # if isinstance(qweight, torch.Tensor):
        #     qweight = qweight.numpy()

        # if isinstance(qzeros, torch.Tensor):
        #     qzeros = qzeros.numpy()

        # Dequantize qweight
        # intweight = np.zeros((qweight.shape[0] * 32 // bit, qweight.shape[1]), dtype=np.int32)
        intweight = torch.zeros((qweight.shape[0] * 32 // bit, qweight.shape[1]), dtype=torch.int32).to(scales.device)
        i = 0
        row = 0
        while row < qweight.shape[0]:
            if bit in [2, 4, 8]:
                for j in range(i, i + (32 // bit)):
                    intweight[j] = (qweight[row] >> (bit * (j - i))) & ((1 << bit) - 1)
                i += 32 // bit
                row += 1
            else:
                raise NotImplementedError("Only 2, 4, 8 bits are supported.")
        # intweight = intweight.astype(np.float32)
        intweight = intweight.to(torch.float32)

        # Dequantize qzeros
        # zeros = np.zeros((qzeros.shape[0], qzeros.shape[1] * 32 // bit), dtype=np.int32)
        zeros = torch.zeros((qzeros.shape[0], qzeros.shape[1] * 32 // bit), dtype=torch.int32)
        i = 0
        col = 0
        while col < qzeros.shape[1]:
            if bit in [2, 4, 8]:
                for j in range(i, i + (32 // bit)):
                    zeros[:, j] = (qzeros[:, col] >> (bit * (j - i))) & ((1 << bit) - 1)
                i += 32 // bit
                col += 1
            else:
                raise NotImplementedError("Only 2, 4, 8 bits are supported.")
        # zeros = zeros.astype(np.float32)
        zeros = zeros.to(torch.float32)
        zeros += 1
        # scale_zeros = (torch.tensor(zeros).to(scales.device) * scales).to(torch.bfloat16)
        # if zeros.shape != scales.shape:
        #     import pdb; pdb.set_trace()
        scale_zeros = (zeros.to(scales.device) * scales).to(torch.bfloat16)

        # Convert intweight and zeros to Tensor
        # intweight = torch.from_numpy(intweight).to(torch.bfloat16)
        # zeros = torch.from_numpy(zeros).to(torch.bfloat16)

        # Reshape intweight to match the original fp16_weight shape
        fp16_weight_shape = (intweight.shape[1],len(g_idx))  
        dequantized_weight = torch.zeros(fp16_weight_shape, dtype=torch.bfloat16)
        for idx in range(fp16_weight_shape[1]):
            dequantized_weight[:, idx] = (intweight[idx, :] * scales[g_idx[idx]] - scale_zeros[g_idx[idx]]).to(torch.bfloat16) 

        return dequantized_weight                 
    
    def pre_dequant(self):
        U = self.dequant(self.bits[0],self.scales_U_4,self.g_idx_U_4,self.qweight_U_4,self.qzeros_U_4).to(self.S_4.device)
        V = self.dequant(self.bits[0],self.scales_V_4,self.g_idx_V_4,self.qweight_V_4,self.qzeros_V_4).to(self.S_4.device)
        
        setattr(self, "U", U)
        setattr(self, "V", V)
    
    def forward(self, x):
        # forward using kernel

        y = x.clone()
        x = x.reshape(-1, x.shape[-1])
        weight_V = gptq_extension.gptq_gemm(x, self.qweight_V_4, self.qzeros_V_4, self.scales_V_4, self.g_idx_V_4, False, self.bits[0])
        x = x @ weight_V @ torch.diag(self.S_4.to(torch.float16))
        
        # out = QuantLinearFunction.apply(x.reshape(-1, x.shape[-1]), self.qweight_V_4, self.scales_V_4, self.qzeros_V_4, self.g_idx_V_4, self.bits[0], self.maxq)
        # out = out
        weight_U = gptq_extension.gptq_gemm(x, self.qweight_U_4, self.qzeros_U_4, self.scales_U_4, self.g_idx_U_4, False, self.bits[0])
        x = x @ weight_U
        
        # out = QuantLinearFunction.apply(out, self.qweight_U_4, self.scales_U_4, self.qzeros_U_4, self.g_idx_U_4, self.bits[0], self.maxq)
        
        '''
        if len(self.S_4) > 512:
            state_dict = torch.load("/data/groups/QY_LLM_Other/pingbowen/models/mathlora/mathlora_train_4.pt")
            weight = state_dict[f'model.layers.0.mlp.down_proj.U_4'] @ torch.diag(state_dict[f'model.layers.0.mlp.down_proj.S_4']) @ state_dict[f'model.layers.0.mlp.down_proj.V_4'].T
            # weight_cuda = weight_V @ torch.diag(self.S_4.to(torch.float16)) @ weight_U
            
            import pdb; pdb.set_trace()
        '''
        
        return (weight_V @ torch.diag(self.S_4.to(torch.float16)) @ weight_U).T

        # U = self.dequant(self.bits[0],self.scales_U_4,self.g_idx_U_4,self.qweight_U_4,self.qzeros_U_4).to(self.S_4.device)
        # V = self.dequant(self.bits[0],self.scales_V_4,self.g_idx_V_4,self.qweight_V_4,self.qzeros_V_4).to(self.S_4.device)
        # x = x @ (self.U @ torch.diag(self.S_4) @ self.V).T.to(x.dtype)
        # return x
    
def make_quant_linear(model, names, bits, groupsize, index_dict,name=''):
    # if isinstance(module, MixquantLinear):
    #     return
        
    for name, module in model.named_modules():   
        if "self_attn" in name or "mlp" in name:
            for subname, submodule in module.named_children():
                if "proj" in subname:
                    tmp = getattr(module, subname)
                    setattr(module, subname, None)
                    gc.collect()
                    torch.cuda.empty_cache()
                    
                    setattr(module, subname, MixquantLinear(bits, groupsize, tmp.V_total.shape[0],tmp.U_total.shape[0], tmp.S_total,index_dict,name=name))                        


def autotune_warmup_linear(model, transpose=False):
    """
    Pre-tunes the quantized kernel
    """
    from tqdm import tqdm

    kn_values = {}

    for _, m in model.named_modules():
        if not isinstance(m, QuantLinear):
            continue

        k = m.infeatures
        n = m.outfeatures

        if (k, n) not in kn_values:
            kn_values[(k, n)] = (m.qweight.cuda(), m.scales.cuda(), m.qzeros.cuda(), m.g_idx.cuda(), m.bits, m.maxq)

    print(f'Found {len(kn_values)} unique KN Linear values.')

    print('Warming up autotune cache ...')
    with torch.no_grad():
        for m in tqdm(range(0, 12)):
            m = 2**m  # [1, 2048]
            for (k, n), (qweight, scales, qzeros, g_idx, bits, maxq) in kn_values.items():
                a = torch.randn(m, k, dtype=torch.float16, device='cuda')
                matmul248(a, qweight, scales, qzeros, g_idx, bits, maxq)
                if transpose:
                    a = torch.randn(m, n, dtype=torch.float16, device='cuda')
                    transpose_matmul248(a, qweight, scales, qzeros, g_idx, bits, maxq)
    del kn_values
