import argparse
import json
import os
import random
from pathlib import Path

import torch  # isort: split
import hipbsolidxgemm
import pandas as pd
import torch.nn.functional as F

hipbsolidxgemm.hipb_create_extension()

rtol = 1e-5
atol = 1


class Fp8Gemm:

    def __init__(self, m, n, k, indtype, outdtype):
        self.m = m
        self.k = k
        self.n = n
        self.indtype = indtype
        self.outdtype = outdtype
        self.nb = 37
        self.inp = torch.randn((self.n, self.k),
                               device='cuda').to(self.indtype)
        self.weights = torch.randn((self.m, self.k),
                                   device='cuda').to(self.indtype)
        # weights2 is used in measurement/warm iters to ensure HBM
        # fetch for weight tensors
        self.weights2 = torch.randn((self.nb, self.m, self.k),
                                    device='cuda').to(self.indtype)
        self.blob = torch.ones(128 * 1024 * 1024,
                               dtype=torch.float32,
                               device='cuda')
        self.topn = 20  #number of top solutions from each source
        self.hipb_sols = []
        self.rtol = 1e-5
        self.atol = 1
        self.start = torch.cuda.Event(enable_timing=True)
        self.end = torch.cuda.Event(enable_timing=True)

    def find_hipblas_sols(self):
        sols = hipbsolidxgemm.hipb_findallsols(self.inp, self.weights.t(),
                                               self.outdtype)
        print('M N K',
              self.m,
              self.n,
              self.k,
              '>>> Total hipb solutions',
              len(sols),
              flush=True)
        #print(sols)
        self.hipb_sols = sols

    def check_gemm_ref(self, libtype, solidx):
        ref = F.linear(self.inp.to(torch.float32),
                       self.weights.to(torch.float32)).to(self.outdtype)
        c = hipbsolidxgemm.hipb_mm(self.inp, self.weights.t(), solidx,
                                   self.outdtype)
        if torch.allclose(c, ref, atol=self.atol, rtol=self.rtol):
            #print('>>>',libtype,'Solidx',solidx,'passed reference test')
            return True
        else:
            print('>>>', 'Solidx', solidx, 'FAILED reference test', flush=True)
            print(ref, flush=True)
            print(c, flush=True)
            return False

    def hipb_time_sol(self, solidx, cold_iters=2, warm_iters=10):
        #print('>>>hipbtime',solidx)
        for i in range(cold_iters):
            hipbsolidxgemm.hipb_mm(self.inp, self.weights.t(), solidx,
                                   self.outdtype)
        self.start.record()
        for i in range(warm_iters):
            hipbsolidxgemm.hipb_mm(
                self.inp, self.weights2[random.randint(0, self.nb - 1)].t(),
                solidx, self.outdtype)
        self.end.record()
        torch.cuda.synchronize()
        gtime = self.start.elapsed_time(self.end) / warm_iters
        #print('>>> Solidx GTime',solidx,gtime,'ms')
        return gtime

    def hipb_time_all_sols(self, fast_mode=0, top_sols=0):
        coldi = 20
        warmi = 20
        if fast_mode:
            coldi = 2
            warmi = 2
        solutions = self.hipb_sols
        if top_sols:
            solutions = self.hipb_top_sols
        gtimes = {}
        for solidx in solutions:
            gtimes[solidx] = self.hipb_time_sol(solidx,
                                                cold_iters=coldi,
                                                warm_iters=warmi)
        self.hipb_gtimedf = pd.DataFrame.from_dict(
            gtimes, orient='index',
            columns=['gtimems']).sort_values(by='gtimems')
        self.hipb_gtimedf.to_csv('/tmp/hipb_gtimedf.csv')
        print('>>> HipBlasLt top solutions, Fast Mode', fast_mode)
        print(self.hipb_gtimedf.head(self.topn))

    def warmup(self, warmi=500):
        for i in range(warmi):
            self.blob = self.blob + 0.00001

    def functional_check_topn_fastest(self):
        hipb_topn = []
        for solidx in self.hipb_gtimedf.index[:self.topn]:
            if self.check_gemm_ref(libtype='hipblaslt', solidx=solidx):
                hipb_topn.append(solidx)
        self.hipb_top_sols = hipb_topn

    def find_fastest_solution(self):
        self.find_hipblas_sols()
        self.warmup()
        self.hipb_time_all_sols(fast_mode=1)
        self.functional_check_topn_fastest()
        self.warmup()
        self.hipb_time_all_sols(fast_mode=0, top_sols=1)
        if len(self.hipb_gtimedf) > 0:
            best_hipb_time = self.hipb_gtimedf.gtimems.iloc[0]
            self.best_solidx = self.hipb_gtimedf.index[0]
            self.best_soltime = best_hipb_time
        else:
            print('>>> No hipblas solutions found!', flush=True)
            self.best_solidx = 0
            self.best_soltime = 0
        print('>>> Fastest Solution is',
              self.best_solidx,
              self.best_soltime,
              flush=True)


class Fp8GemmTuner:

    def __init__(self, indtype, outdtype, tuned_file=None):
        self.gemm_problems = pd.DataFrame(columns=['M', 'N', 'K'])
        self.indtype = indtype
        self.outdtype = outdtype
        self.tuned_file = tuned_file
        if Path(tuned_file).is_file():
            self.gdf = pd.read_csv(tuned_file)
        else:
            self.gdf = None

    def add_gemm(self, m, n, k):
        if (self.gdf is None
                or (self.gdf[(self.gdf['M'] == m) & (self.gdf['N'] == n) &
                             (self.gdf['K'] == k)].empty)):
            entry = {'M': [m], 'N': [n], 'K': [k]}
            df = pd.DataFrame(entry)
            self.gemm_problems = pd.concat([self.gemm_problems, df],
                                           ignore_index=True)
        else:
            print(
                f">>>Info: Found Duplicate shape(M:{m}, N:{n}, K:{k}), skipping"
            )

    def find_best_sols(self):
        df = self.gemm_problems
        soldf = pd.DataFrame()
        for i in range(len(df)):
            ds = df.iloc[i]
            gemmobj = Fp8Gemm(ds['M'],
                              ds['N'],
                              ds['K'],
                              indtype=self.indtype,
                              outdtype=self.outdtype)
            gemmobj.find_fastest_solution()
            soldf.loc[i, 'solidx'] = gemmobj.best_solidx
            soldf.loc[i, 'soltimems'] = gemmobj.best_soltime
        soldf['indtype'] = self.indtype
        soldf['outdtype'] = self.outdtype
        finaldf = pd.concat([self.gemm_problems, soldf], axis=1)
        finaldf = pd.concat([finaldf, self.gdf])
        finaldf.to_csv(self.tuned_file, index=False)
        print(finaldf)


def generate_mk_sets(model_dir, tp=1):
    with open(f'{model_dir}/config.json') as f:
        data = json.load(f)
    hidden_size = data['hidden_size']
    intermediate_size = data['intermediate_size']
    total_num_heads = data['num_attention_heads']
    total_num_kv_heads = data['num_key_value_heads']
    head_dim = hidden_size // total_num_heads
    return [((total_num_heads + (2 * total_num_kv_heads)) * head_dim // tp,
             hidden_size), (hidden_size, hidden_size // tp),
            (intermediate_size * 2 // tp, hidden_size),
            (hidden_size, intermediate_size // tp)], hidden_size


def get_dtype(dtype_str):
    dtype = torch.float8_e4m3fnuz
    if dtype_str == 'f32':
        dtype = torch.float32
    elif dtype_str == 'bf16':
        dtype = torch.bfloat16
    elif dtype_str == 'f16':
        dtype = torch.float16
    elif dtype_str == 'f8':
        dtype = torch.float8_e4m3fnuz
    else:
        print('>>> Warning! Invalid dtype', dtype_str,
              'using default dtype f8')
    return dtype


def list_of_ints(arg):
    return list(map(int, arg.split(',')))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("--model_dir",
                        type=str,
                        default=os.getenv('GTUNE_MODEL', ""),
                        help="Enter the location of your model directory")
    parser.add_argument("--tuned_file",
                        type=str,
                        default=os.getenv('GTUNE_TUNED', "tuned.csv"),
                        help="output file for tuned gemm solutions")
    parser.add_argument(
        "--input_file",
        type=str,
        default=os.getenv('GTUNE_INPUT', None),
        help="list of gemms to tune for, mutually exclusive with model_dir")
    parser.add_argument("--tp",
                        type=int,
                        default=os.getenv('GTUNE_TP', 1),
                        help="Tensor parallelism to be used.")
    parser.add_argument("--indtype",
                        type=str,
                        default='f8',
                        help="dtype f32 f16 bf16 fp8")
    parser.add_argument("--outdtype",
                        type=str,
                        default='f16',
                        help="dtype f32 f16 bf16 fp8")
    parser.add_argument("--batch_size",
                        type=int,
                        default=os.getenv('GTUNE_BATCH_SIZE', 1),
                        help="Batch size to tune for")
    parser.add_argument("--nsets",
                        type=list_of_ints,
                        default=[1, 512, 1024, 2048, 3072, 4096, 8192, 16384],
                        help="N sizes to tune for: 1,128,2048")
    args = parser.parse_args()

    indtype = get_dtype(args.indtype)
    outdtype = get_dtype(args.outdtype)

    gtuner = Fp8GemmTuner(indtype, outdtype, args.tuned_file)
    nsets = [i * args.batch_size for i in args.nsets]
    if args.input_file:
        print(f">>> Loading {args.input_file}")
        if not Path(args.input_file).is_file():
            print(f">>> ERROR: {args.input_file} does not exist.  Exiting")
            exit(1)
        shapes = pd.read_csv(args.input_file)
        for i in range(len(shapes)):
            ds = shapes.iloc[i]
            gtuner.add_gemm(ds['M'], ds['N'], ds['K'])
    else:
        if not args.model_dir:
            print(">>> Warning! NO MODEL SPECIFIED. Tuning for LL2 13B TP1")
            #LL2 13B sizes
            mksets = [(15360, 5120), (5120, 5120), (27648, 5120),
                      (5120, 13824)]
            gtuner.add_gemm(m=32000, n=1, k=5120)  # logits gemm
        else:
            mksets, hidden_size = generate_mk_sets(args.model_dir, args.tp)
            gtuner.add_gemm(
                m=32000 // args.tp, n=1 * args.batch_size, k=hidden_size
            )  #TODO: Handle cases where vocab_size is not divisible by tp

        for n in sorted(nsets):
            for m, k in mksets:
                gtuner.add_gemm(m, n, k)

    gtuner.find_best_sols()
