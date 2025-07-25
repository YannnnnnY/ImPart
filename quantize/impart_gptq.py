import math
import time

import torch
import torch.nn as nn
import transformers
import quant
from texttable import Texttable
from utils import torch_snr_error

torch.backends.cuda.matmul.allow_tf32 = False
torch.backends.cudnn.allow_tf32 = False


class Observer:

    def __init__(self, topk=32):
        self.loss_list = []
        self.topk = topk

    def submit(self, name: str, layerid: int, gptq, error: float):

        item = (name, layerid, {'gptq': gptq, 'error': error})

        if len(self.loss_list) < self.topk:
            self.loss_list.append(item)
            return

        min_error = error
        min_idx = -1
        for idx, data in enumerate(self.loss_list):
            if min_error > data[2]['error']:
                min_idx = idx
                min_error = data[2]['error']

        if min_idx >= 0:
            self.loss_list[min_idx] = item

    def print(self):
        self.loss_list = sorted(self.loss_list, key=lambda s: s[2]['error'], reverse=True)

        table = Texttable()

        table.header(['name', 'error'])
        table.set_cols_dtype(['t', 'f'])

        for item in self.loss_list:
            table.add_row([f"{item[0]}.{item[1]}", item[2]['error']])
        print(table.draw())
        print('\n')

    def items(self):
        return self.loss_list


class GPTQ:

    def __init__(self, layer, quant_type, observe=False):
        self.layer = layer
        self.dev = self.layer.V_total.device
        self.quant_type = quant_type
        # W = layer.weight.data.clone()
        self.weights = None

        W = layer.V_total.T.data.clone() if self.quant_type == 'V' else layer.U.data.clone()  # origin

        if isinstance(self.layer, nn.Conv2d):
            W = W.flatten(1)
        if isinstance(self.layer, transformers.Conv1D):
            W = W.t()
        self.rows = W.shape[0]
        self.columns = W.shape[1]
        self.inp1 = None
        self.out1 = None

        self.H = torch.zeros((self.columns, self.columns), device=self.dev)
        #
        self.nsamples = 0
        self.quantizer = quant.Quantizer()
        self.observe = observe

    def add_single(self, inp, out):
        assert self.observe == False
        if len(inp.shape) == 2:
            inp = inp.unsqueeze(0)
        tmp = inp.shape[0]
        inp = inp.sum(dim=0)
        self.H *= self.nsamples / (self.nsamples + tmp)
        self.nsamples += tmp
        self.H = 2 / self.nsamples * inp.float() + self.H

    def add_batch(self, inp, out):
        # Hessian H = 2 X XT + λ I

        if self.observe:
            self.inp1 = inp
            self.out1 = out
        else:
            self.inp1 = None
            self.out1 = None

        if len(inp.shape) == 2:
            inp = inp.unsqueeze(0)

        tmp = inp.shape[0]
        inp = inp.reshape((-1, inp.shape[-1]))
        inp = inp.t()

        if isinstance(self.layer, nn.Linear) or isinstance(self.layer, transformers.Conv1D):
            if len(inp.shape) == 3:
                inp = inp.reshape((-1, inp.shape[-1]))
            inp = inp.t()
        if isinstance(self.layer, nn.Conv2d):
            unfold = nn.Unfold(self.layer.kernel_size, dilation=self.layer.dilation, padding=self.layer.padding,
                               stride=self.layer.stride)
            inp = unfold(inp)
            inp = inp.permute([1, 0, 2])
            inp = inp.flatten(1)
        self.H *= self.nsamples / (self.nsamples + tmp)
        self.nsamples += tmp
        # inp = inp.float()
        inp = math.sqrt(2 / self.nsamples) * inp.float()
        # self.H += 2 / self.nsamples * inp.matmul(inp.t())
        self.H += inp.matmul(inp.t())

    def print_loss(self, name, q_weight, weight_error, timecost, bit, channel):
        table = Texttable()
        name += ' ' * (16 - len(name))
        bit = str(bit)
        bit += ' ' * (2 - len(bit))
        channel = str(channel)
        channel += ' ' * (4 - len(channel))
        # if self.quant_type == "V":
        table.header(['name', 'weight_error', 'fp_inp_SNR', 'q_inp_SNR', 'time', "ranks", "bit"])
        # else:
        #     table.header(['name', 'weight_error', 'fp_inp_SNR', 'q_inp_SNR', 'time', "bit"])

        # assign weight
        if self.quant_type == 'V':
            self.layer.V.data = q_weight.reshape(self.layer.V.data.T.shape).to(self.layer.V.data.dtype).T  # origin
        else:
            self.layer.U.data = q_weight.reshape(self.layer.U.data.shape).to(self.layer.U.data.dtype)

        if self.inp1 is not None:
            # quantize input to int8
            quantizer = quant.Quantizer()
            quantizer.configure(8, perchannel=False, sym=True, mse=False)
            quantizer.find_params(self.inp1)
            q_in = quantizer.quantize(self.inp1).type(torch.float16)
            q_out = self.layer(q_in)

            # get kinds of SNR
            q_SNR = torch_snr_error(q_out, self.out1).item()
            fp_SNR = torch_snr_error(self.layer(self.inp1), self.out1).item()
        else:
            q_SNR = '-'
            fp_SNR = '-'
        # if self.quant_type == "V":
        # import pdb;pdb.set_trace()
        table.add_row([name, weight_error, fp_SNR, q_SNR, timecost, channel, bit])
        # else:
        #     table.add_row([name, weight_error, fp_SNR, q_SNR, timecost, bit])
        print(weight_error)
        print(table.draw().split('\n')[-2])

        # import pdb; pdb.set_trace()

    def fasterquant(self, blocksize=128, percdamp=.01, groupsize=-1, actorder=False, name='', bit=None):
        # import pdb; pdb.set_trace()

        assert bit is not None, "bit should not be None"
        self.layer.to(self.dev)

        W = self.layer.V.data.T.clone() if self.quant_type == 'V' else self.layer.U.data.clone()  # origin
        W_mask = self.layer.V_mask.data.T.clone() if self.quant_type == 'V' else self.layer.U_mask.data.clone()  # origin

        W = W.float()

        tick = time.time()

        # if not self.quantizer.ready():
        self.quantizer.find_params(W, weight=True)

        H = self.H.clone()  # 重复利用

        dead = torch.diag(H) == 0
        H[dead, dead] = 1
        W[:, dead] = 0

        if actorder:
            perm = torch.argsort(torch.diag(H), descending=True)
            W = W[:, perm]
            W_mask = W_mask[:, perm]
            H = H[perm][:, perm]

        Losses = torch.zeros_like(W)
        Q = torch.zeros_like(W)

        damp = percdamp * torch.mean(torch.diag(H))
        diag = torch.arange(self.columns, device=self.dev)
        H[diag, diag] += damp
        H = torch.linalg.cholesky(H)
        H = torch.cholesky_inverse(H)
        H = torch.linalg.cholesky(H, upper=True)
        Hinv = H

        g_idx = []
        scale = []
        zero = []
        now_idx = 1  # W[rank, input]
        # import pdb; pdb.set_trace()
        for i1 in range(0, self.columns, blocksize):
            # import pdb; pdb.set_trace()
            i2 = min(i1 + blocksize, self.columns)
            count = i2 - i1

            W1 = W[:, i1:i2].clone()
            W_mask1 = W_mask[:, i1:i2].clone()
            Q1 = torch.zeros_like(W1)
            Err1 = torch.zeros_like(W1)
            Losses1 = torch.zeros_like(W1)
            Hinv1 = Hinv[i1:i2, i1:i2]

            for i in range(count):
                # import pdb; pdb.set_trace()
                w_mask1 = W_mask1[:, i]

                w = W1[:, i] * w_mask1  # e.g. * mask 是后续加上的，防止0被补偿得太大，导致影响quant # [output, input_i]
                # w = W1[:, i]

                # if not W1[:, i].equal(W1[:, i] * w_mask1):
                #     import pdb; pdb.set_trace()
                #     print("="*50)
                #     print("Not equal", W1[:, i] - W1[:, i] * w_mask1)
                #     print(f"W1: {W1}")
                #     print(f"W_mask1: {W_mask1}")
                #     (W1[:, i] != W1[:, i] * w_mask1).nonzero(as_tuple=True)
                d = Hinv1[i, i]

                if groupsize != -1:
                    if (i1 + i) % groupsize == 0:
                        self.quantizer.find_params(W[:, (i1 + i):(i1 + i + groupsize)], weight=True)

                    if ((i1 + i) // groupsize) - now_idx == -1:
                        scale.append(self.quantizer.scale)
                        zero.append(self.quantizer.zero)
                        now_idx += 1

                q = self.quantizer.quantize(w.unsqueeze(1)).flatten() * w_mask1
                Q1[:, i] = q # * w_mask1
                Losses1[:, i] = (w - q) ** 2 / d ** 2

                err1 = (w - q) / d
                W1[:, i:] -= err1.unsqueeze(1).matmul(Hinv1[i, i:].unsqueeze(0))
                Err1[:, i] = err1

            Q[:, i1:i2] = Q1
            Losses[:, i1:i2] = Losses1 / 2

            W[:, i2:] -= Err1.matmul(Hinv[i1:i2, i2:])
        torch.cuda.synchronize()
        if self.quant_type == "V" and self.weights is not None:
            Losses = Losses * self.weights.pow(2).unsqueeze(1)
        error = torch.sum(Losses).item()

        groupsize = groupsize if groupsize != -1 else self.columns
        g_idx = [i // groupsize for i in range(self.columns)]
        g_idx = torch.tensor(g_idx, dtype=torch.int32, device=Q.device)
        if actorder:
            invperm = torch.argsort(perm)
            Q = Q[:, invperm]
            g_idx = g_idx[invperm]

        if isinstance(self.layer, transformers.Conv1D):
            Q = Q.t()

        self.print_loss(name=name, q_weight=Q, weight_error=error, timecost=(time.time() - tick), bit=bit,
                        channel=Q.shape[0] if self.quant_type == "V" else H.shape[0])
        if scale == []:
            scale.append(self.quantizer.scale)
            zero.append(self.quantizer.zero)
        scale = torch.cat(scale, dim=1)
        zero = torch.cat(zero, dim=1)
        return scale, zero, g_idx, error  # , Q

    def get_quant_loss(self, blocksize=128, percdamp=.01, groupsize=-1, actorder=False, name='',
                       quants=torch.arange(0, 17), sym=True):
        weights = self.weights.float().clone()
        origin_W = self.layer.V.data.T.clone() if self.quant_type == 'V' else self.layer.U.data.clone()  # origin

        self.layer.to("cpu")
        assert weights is None or (
                    isinstance(weights, torch.Tensor) and weights.dim() == 1 and weights.size(0) == origin_W.shape[0])
        if weights is not None:
            weights = weights.pow(2).unsqueeze(0)
        else:
            weights = torch.ones(weights.size(0), dtype=torch.float32, device=self.dev).unsqueeze(0)

        if isinstance(self.layer, nn.Conv2d):
            origin_W = origin_W.flatten(1)
        if isinstance(self.layer, transformers.Conv1D):
            origin_W = origin_W.t()
        origin_W = origin_W.float()

        tick = time.time()

        H = self.H
        #  if not self.observe:
        # del self.H
        dead = torch.diag(H) == 0
        H[dead, dead] = 1
        dead = dead[:origin_W.shape[-1]]
        origin_W[:, dead] = 0

        if actorder:
            perm = torch.argsort(torch.diag(H), descending=True)
            origin_W = origin_W[:, perm]
            H = H[perm][:, perm]
        damp = percdamp * torch.mean(torch.diag(H))
        diag = torch.arange(self.columns, device=self.dev)
        H[diag, diag] += damp
        H = torch.linalg.cholesky(H)
        H = torch.cholesky_inverse(H)
        H = torch.linalg.cholesky(H, upper=True)
        all_loss = []
        self.quantizer = {}
        for bit in quants:
            bit = int(bit)
            if bit != 0:
                self.quantizer[bit] = quant.Quantizer()
                self.quantizer[bit].configure(bit, perchannel=True, sym=sym, mse=False)
                self.quantizer[bit].find_params(origin_W, weight=True)

        batch_size = 17
        for bit_i in range(0, len(quants), batch_size):
            W = origin_W.clone().unsqueeze(0).repeat(len(quants[bit_i:bit_i + batch_size]), 1,
                                                     1)  # [batch, output, input]
            Losses = torch.zeros_like(W)  # [batch, output, input]
            Hinv = H.clone()
            for i1 in range(0, self.columns, blocksize):
                i2 = min(i1 + blocksize, self.columns)
                count = i2 - i1

                W1 = W[..., :, i1:i2].clone()  # [batch, output, blocksize]
                Err1 = torch.zeros_like(W1)  # [batch, output, blocksize]
                Losses1 = torch.zeros_like(W1)  # [batch, output, blocksize]
                Hinv1 = Hinv[i1:i2, i1:i2]  # [blocksize, blocksize]

                for i in range(count):
                    w = W1[..., :, i]  # [batch, output, input_i]
                    d = Hinv1[i, i]
                    qs = []
                    for idx, bit in enumerate(quants[bit_i:bit_i + batch_size]):
                        bit = int(bit)
                        if bit != 0:
                            if groupsize != -1:
                                if (i1 + i) % groupsize == 0:
                                    self.quantizer[bit].find_params(W[idx, :, (i1 + i):(i1 + i + groupsize)],
                                                                    weight=True)

                            q = self.quantizer[bit].quantize(w[idx].unsqueeze(1)).flatten()
                        else:
                            q = torch.zeros_like(w[idx])
                        qs.append(q)
                    if len(qs) > 1:
                        q = torch.stack(qs, dim=0)
                    else:
                        q = qs[0].unsqueeze(0)
                    Losses1[..., :, i] = (w - q) ** 2 / d ** 2 * weights / 2  # [batch, output]

                    err1 = (w - q) / d  # [batch, output]
                    # Hinv1[i, i:] -> [ blocksize] -> [1, blocksize] -> [1, 1, blocksize]
                    # err1 -> [batch, output] -> [batch, output, 1]
                    W1[..., :, i:] -= err1.unsqueeze(-1).matmul(
                        Hinv1[i, i:].unsqueeze(0).unsqueeze(0))  # [batch, output, blocksize]
                    Err1[..., :, i] = err1

                Losses[..., :, i1:i2] = Losses1
                W[..., :, i2:] -= Err1.matmul(Hinv[i1:i2, i2:].unsqueeze(
                    0))  # [batch, output, blocksize] , [1, blocksize, last] -> [batch, output, last]
            torch.cuda.synchronize()
            all_loss.append(Losses.sum(dim=-1))  # [batch, output, input] -> [batch, output]
        print("cost time(s):", time.time() - tick)
        self.layer.to(self.dev)
        return torch.cat(all_loss, dim=0).T  # [quant, output] -> [output, quant]

    def free(self):
        self.inp1 = None
        self.out1 = None
        self.H = None
        self.Losses = None
        self.Trace = None
        torch.cuda.empty_cache()
