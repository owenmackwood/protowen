import numpy as np
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec
import matplotlib.cm as cm
from typing import Callable, List, NamedTuple, Union, Tuple
from pathlib import Path
from enum import Enum
import time
import torch
from torch.utils.tensorboard import SummaryWriter
from numba import njit
import argparse

test_correlation = True

n_input: int = 2 if test_correlation else 5
n_hidden: int = 243
n_output: int = 3

calibration_file = Path.home() / "calibrations.npz"
targets = {
    "leak": 80,
    "reset": 80,
    "threshold": 150,
    "tau_mem": 6e-6,
    "tau_syn": 6e-6,
    "i_synin_gm": 500,
    "membrane_capacitance": 63,
    "refractory_time": 2e-6
}

class Classifier(Enum):
    first_spike = 1
    spike_count = 2
    potential = 3


class SampleMADC(Enum):
    off = 0
    membrane = 1
    exc_synin = 2


class HyperParams(NamedTuple):
    r_small: float
    gamma0: float
    lambda0: float
    batch_size: int
    train_size: int
    test_size: int
    epochs: int
    tau_stdp: float
    eta: float
    lr_step_size: int
    lr_gamma: float
    regularize_per_sample: bool
    input_repetitions: int
    w_hidden_scale: float
    w_output_scale: float
    w_hidden_mean: float
    w_output_mean: float
    spike_target_hidden: float
    spike_target_output: float
    refractory_hidden: float
    refractory_output: float
    softmax_nu: float


class TbOptions(NamedTuple):
    log_each_batch: bool
    log_class_images: bool
    log_noclass_images: bool


class RunResult(NamedTuple):
    loss: np.ndarray
    accuracy: np.ndarray


class ForwardResult(NamedTuple):
    loss: float
    accuracy: float
    t_backend: float
    t_traces: float
    t_weight_update: float


optimize_hyperparameters = False
print_multispike_warning = False
measure_hw_correlation = True
madc_rec = SampleMADC.off

classifier = Classifier.first_spike

if classifier == Classifier.first_spike:
    refractory_output = 20e-6
    refractory_hidden = 1e-6
    targets["refractory_time"] = {
        0: refractory_hidden,
        2*n_hidden: refractory_output
    }

elif classifier == Classifier.spike_count:
    targets["refractory_time"] = refractory_output = refractory_hidden = 0.04e-6

else:
    targets["reset"] = {
        0: 80,
        2*n_hidden: 145
    }
    targets["refractory_time"] = refractory_output = refractory_hidden = 0.04e-6


def main(wafer: int, fpga: int, log_dir: Path, optimize_hyperparameters: bool):
    import shutil
    from calibrate import get_wafer_calibration
    import nevergrad as ng

    # import torch
    # from dlens_vx_v2 import logger
    # logger_fisch = logger.get("fisch")
    # logger.set_loglevel(logger_fisch, logger.LogLevel.DEBUG)
    # logger.default_config(level=logger.LogLevel.DEBUG)
    # logger.append_to_file("all.log")

    shutil.copy(__file__, log_dir)

    # weight_scale: float = 4 * 240.  # 1000.  # 
    # scale = 2.5 * 240. * 0.7 * (1.0 - np.exp(-1.7e-6/6e-6))
    interpolation: int = 1
    n_samples: int = 1

    # alignment of traces and spikes from chip
    spike_shift = 1.7e-6 / interpolation

    seed = 2

    # epochs = 20
    # batch_size = 100
    # train_size = batch_size*50
    # test_size = batch_size*20

    epochs = 2
    batch_size = 10
    train_size = batch_size*50
    test_size = batch_size*20


    regularize_per_sample = True
    use_r1_reg = True
    hw_scale = 240.
    w_max = 63 / hw_scale
    w_scale = 0.7 * (1.0 - np.exp(-1.7e-6/6e-6))

    input_repetitions: int = 25

    w_hidden_mean: float = 15. / hw_scale  # 30
    w_output_mean: float = 6.8 / hw_scale  # 5
    w_hidden_scale: float = 1.21 / np.sqrt(n_input * input_repetitions)  # 0.10800539276323406 
    w_output_scale: float = 0.331 / np.sqrt(n_hidden)  # 0.02124551864526359 

    eta: float = 0.  # 1.5e-3
    spike_target_hidden: float = 1. / n_output
    spike_target_output: float = 1.
    reset_cadc_each_sample: bool = False  # Should be set to True if recording membrane values for the entire sample, i.e. n_samples * spike_shift ~ sample_separation
    use_best_epoch: bool = False

    hpd = dict(
        # lr_step_size=ng.p.Scalar(init=lr_step_size, lower=1, upper=epochs).set_integer_casting(),
        # eta=ng.p.Scalar(init=eta, lower=1e-3, upper=1e-2),
        # lr_step_size=ng.p.Choice([5, 10, 15, epochs]),
        # lr_gamma=ng.p.Scalar(init=.9, lower=.5, upper=.9999),
        # tau_stdp=ng.p.Scalar(init=hp.tau_stdp, lower=.5*hp.tau_stdp, upper=1.5*hp.tau_stdp),
        # r_small=ng.p.Scalar(init=hp.r_small, lower=1., upper=3.),
        # regularize_per_sample=ng.p.Choice([True, False]),
        # gamma0=ng.p.Log(init=hp.gamma0, lower=1e-5, upper=1.),
        # lambda0=ng.p.Log(init=hp.lambda0, lower=1e-7, upper=1.),
        # softmax_nu=ng.p.Scalar(init=softmax_nu, lower=softmax_nu_min, upper=softmax_nu_max),
        # w_hidden_mean=ng.p.Scalar(init=hp.w_hidden_mean, lower=-1./hw_scale, upper=40./hw_scale),
        # w_output_mean=ng.p.Scalar(init=hp.w_output_mean, lower=-5./hw_scale, upper=10./hw_scale),
        # w_hidden_scale=ng.p.Scalar(init=hp.w_hidden_scale, lower=hp.w_hidden_scale/10, upper=hp.w_hidden_scale*10),
        # w_output_scale=ng.p.Scalar(init=hp.w_output_scale, lower=hp.w_output_scale/10, upper=hp.w_output_scale*10),
    )

    if classifier == Classifier.first_spike:
        {'w_hidden_mean': 0.062477176414907555, 'w_output_mean': 0.028288758116673557, 
        'w_hidden_scale': 0.10800539276323406, 'w_output_scale': 0.02124551864526359}

        r1_power = 2
        r_small = 1.8
        tau_stdp = 6.8e-6  # targets["tau_syn"]

        gamma0 = 3e-4  # Min spikes regularization
        lambda0 = 6e-6  # Firing rate regularization
        softmax_nu = 4.5
        softmax_nu_min = 2.
        softmax_nu_max = 10.

    elif classifier == Classifier.spike_count:
        {'r_small': 2.152963316510742, 'tau_stdp': 6.947731277155818e-06, 
        'gamma0': 5.588439934442347e-05, 'lambda0': 7.219428197774032e-05, 
        'softmax_nu': -2.5573550613870677}
        {'lr_step_size': 25, 'lr_gamma': 0.8056618223390083}
        
        r1_power = 0
        r_small = 2.15
        tau_stdp = 7e-6

        gamma0 = 1e-2  # 5.6e-5  # Min spikes regularization
        lambda0 = 1e-3  # 7.2e-5  # Firing rate regularization

        softmax_nu = -2.6
        softmax_nu_min = -10.
        softmax_nu_max = -.1

        # hpd.update(
        #     regularize_per_sample=ng.p.Choice([True, False]),
        #     gamma0=ng.p.Log(init=gamma0, lower=1e-5, upper=1.),
        #     lambda0=ng.p.Log(init=lambda0, lower=1e-7, upper=1.),
        # )

    else:
        n_samples = 25 # 42.5 us
        reset_cadc_each_sample = True

        r1_power = 0
        r_small = 2.15  # total -> 51.5 us
        tau_stdp = 7e-6
        # What about "residual" layer? i.e. feed inputs to output layer.
        # What about log(spikes) for regularization?
        # What about re-deriving rule from membrane potential classification?
        # optimal kernel? tau_s**2  / (2*(tau_s + tau_m))

        gamma0 = 1e-3  # Min spikes regularization
        lambda0 = 1e-5  # L2 regularization

        softmax_nu = -1.  # -2.6

        # hpd.update(
        #     gamma0=ng.p.Log(init=gamma0, lower=1e-5, upper=1.),
        #     lambda0=ng.p.Log(init=lambda0, lower=1e-7, upper=1.),
        # )

    if test_correlation:
        r_small = 10.
        input_repetitions = 16  # 128 // n_input

    r_big = r_small*5
    max_refractory: float = max(refractory_output, refractory_hidden)
    input_shift: float = spike_shift
    sample_separation: float = input_shift + 2*2e-6*r_big + max_refractory + 10e-3  # for CADC read-out (takes about 2.88 ms) plus reset (something >5ms probably).
    # if reset_cadc_each_sample:
    #     input_shift += max_refractory
    #     sample_separation += input_shift + 2e-6*r_big * 2.
    #     n_samples = int(np.ceil(sample_separation / spike_shift))
    n_steps = n_samples * interpolation
    print(f"{input_shift=:.3e} {sample_separation=:.3e} {n_samples=}")

    calibration = get_wafer_calibration(calibration_file, wafer, fpga, targets)
    lr_step_size = 25
    lr_gamma = 0.999

    hp = HyperParams(
        r_small, gamma0, lambda0,
        batch_size, train_size, test_size, epochs, tau_stdp,
        eta, lr_step_size, lr_gamma, regularize_per_sample,
        input_repetitions, w_hidden_scale, w_output_scale, w_hidden_mean, w_output_mean,
        spike_target_hidden, spike_target_output,
        refractory_hidden, refractory_output, softmax_nu,
    )
    run_args = hp._asdict()
    for k in ("refractory_hidden", "refractory_output"):
        del run_args[k]

    run_args.update(
        n_times=1, calibration=calibration, log_dir=log_dir, seed=seed, w_max=w_max,
        hw_scale=hw_scale, input_shift=input_shift, sample_separation=sample_separation,
        use_r1_reg=use_r1_reg, r1_power=r1_power, n_steps=n_steps, r_big=5.*r_small,
        interpolation=interpolation, reset_cadc_each_sample=reset_cadc_each_sample,
    )
    t_start = time.time()
    if optimize_hyperparameters:
        with SummaryWriter(log_dir / "hp") as tb:            

            instrum = ng.p.Instrumentation(**hpd)
            optimizer = ng.optimizers.NGOpt(parametrization=instrum, budget=1000, num_workers=1)

            def str_fmt(k, v) -> str:
                if k in ("gamma0", "lambda0"):
                    return f"{k}={v:.3e}"
                elif k in ("r_small"):
                    return f"{k}={v:.3f}"
                else:
                    return f"{k}={v}"
            
            for o in range(optimizer.budget):
                t_opt_start = time.time()

                x = optimizer.ask()
                run_args.update(log_dir=log_dir / ", ".join(str_fmt(k, v) for k, v in x.kwargs.items()), **x.kwargs)
                if "r_small" in x.kwargs:
                    r_big = 5. * x.kwargs["r_small"]
                    sample_separation=input_shift + 3*2e-6*r_big + max_refractory
                    print(f"{2*r_big=:.2f} {sample_separation=:.3e}")
                    run_args.update(r_big=r_big, sample_separation=sample_separation)

                rr = run_n_times(tb_options=TbOptions(False, False, False), **run_args)  # *x.args, **x.kwargs)

                loss = rr.loss.min() if use_best_epoch else rr.loss[-1]
                optimizer.tell(x, loss)
                tb.add_hparams(
                    {k: v*1e6 if k in ("tau_stdp",) else v for k, v in x.kwargs.items()}, 
                    {"hp/accuracy": rr.accuracy.max() if use_best_epoch else rr.accuracy[-1], 
                    "hp/loss": loss}
                )

                recommendation = optimizer.provide_recommendation()
                print(f"Recommendation {o}: {recommendation.value}")
                tb.add_scalar("time/opt_step", time.time() - t_opt_start, o)

    else:
        run_args["n_times"] = 1
        run_n_times(tb_options=TbOptions(True, True, True), **run_args)
    
    print(f"Total run time: {(time.time() - t_start)/60: .1f} minutes.")


def run_n_times(
    n_times: int,
    calibration, log_dir: Path,
    tb_options: TbOptions,
    seed: int,
    batch_size: int,
    train_size: int,
    test_size: int,
    w_hidden_mean: float,
    w_output_mean: float,
    w_hidden_scale: float,
    w_output_scale: float,
    softmax_nu: float,
    input_repetitions: int,
    eta: float,
    lr_step_size: int,
    lr_gamma: float,
    w_max: float,
    hw_scale: float,
    input_shift: float,
    sample_separation: float,
    r_big: float,
    r_small: float,
    regularize_per_sample: bool,
    use_r1_reg: bool,
    r1_power: float,
    spike_target_hidden: float,
    spike_target_output: float,
    tau_stdp: float,
    gamma0: float,
    lambda0: float,
    epochs: int,
    n_steps: int,
    interpolation: int,
    reset_cadc_each_sample: bool,
) -> RunResult:
    import pyhxcomm_vx as hxcomm
    from functools import partial
    from strobe.datasets.yinyang import YinYangDataset
    from strobe.backend import FPGA_MEMORY_SIZE, StrobeBackend, LayerSize

    synapse_bias: int = 1000
    # fix seed
    np.random.seed(seed)
    test_accuracy = np.empty(epochs)
    test_loss = np.empty(epochs)

    for n in range(n_times):
        
        curr_log_dir = log_dir / f"{n}" if n_times > 1 else log_dir
        
        with hxcomm.ManagedConnection() as connection, SummaryWriter(curr_log_dir) as tb:

            if test_correlation:
                w0 = 63. / hw_scale
                weights_hidden = np.zeros((n_input * input_repetitions, n_hidden))
                weights_output = np.zeros((n_hidden, n_output))
                weights_hidden[0:: n_input, :n_hidden//2] = w0
                weights_hidden[1:: n_input, n_hidden//2:] = w0
                # for i in range(n_hidden):
                #     start = i % n_input  # (i * n_input) % weights_hidden.shape[0]
                #     weights_hidden[start : : n_input, i] = w0

                weights_output[:n_hidden//2, 0] = w0
                weights_output[n_hidden//2:, 1] = w0
                # weights_output[0::n_input, 0] = w0
                # weights_output[2::n_input, 0] = w0
                # weights_output[1::n_input, 1] = w0
                # weights_output[3::n_input, 1] = w0
                # weights_output[4::n_input, 2] = w0

                from torch.utils.data.dataset import Dataset
                class YinYangDataset(Dataset):
                    def __init__(self, r_small, r_big, size, seed):
                        self.r_big = r_big
                        self.r_small = r_small
                        vals = np.linspace(2., 2*r_big-2., n_input)
                        self.__vals = [vals.copy() for _ in range(size)]
                        self.class_names = ['yin', 'yang', 'dot']
                    def __getitem__(self, index):
                        return self.__vals[index], 0
                    def __len__(self):
                        return len(self.__vals)

            else:
                weights_hidden = np.random.normal(
                    size=(n_input * input_repetitions, n_hidden), 
                    loc=w_hidden_mean,
                    scale=w_hidden_scale
                )

                weights_output = np.random.normal(
                    size=(n_hidden, n_output), 
                    loc=w_output_mean,
                    scale=w_output_scale
                )

            weight_layers: List[np.ndarray] = [weights_hidden, weights_output]

            structure: List[Union[int, LayerSize]] = [
                n_input * input_repetitions,
                LayerSize(n_hidden, spiking=True),
                LayerSize(n_output, spiking=True),
            ]

            backend = StrobeBackend(connection, structure, calibration, synapse_bias, sample_separation, measure_hw_correlation)
            backend.configure()
            if madc_rec != SampleMADC.off:
                backend.set_readout(120, madc_rec.name)

            # backend.load_ppu_program(Path(__file__).parent / "../../bin/strobe.bin")
            backend.load_ppu_program(f'{Path.home()/"workspace/bin/strobe.bin"}')

            # load data set
            data_train = YinYangDataset(r_small, r_big, size=train_size, seed=seed)
            data_test = YinYangDataset(r_small, r_big, size=test_size, seed=seed+1)

            train_loader = torch.utils.data.DataLoader(data_train, batch_size=batch_size, shuffle=True)
            test_loader = torch.utils.data.DataLoader(data_test, batch_size=len(data_test), shuffle=False)

            max_hw_batch_size = int(np.floor(FPGA_MEMORY_SIZE / n_steps / backend._n_vectors / 128))
            max_hw_batch_size //= 16
            print(f"Max batch size: {max_hw_batch_size}")

            m_output = np.zeros_like(weights_output)
            v_output = np.zeros_like(weights_output)
            m_hidden = np.zeros_like(weights_hidden)
            v_hidden = np.zeros_like(weights_hidden)

            def nu(epoch: int, epochs: int) -> float:
                return softmax_nu  # softmax_start + (softmax_end - softmax_start) * (epoch / epochs)

            forward_p = partial(
                forward, backend, tb, tb_options, weight_layers, 
                m_output, v_output, m_hidden, v_hidden, 
                max_hw_batch_size, input_repetitions, 
                eta, lr_step_size, lr_gamma, w_max, hw_scale, 
                input_shift, sample_separation, 
                r_big, r_small, 
                regularize_per_sample, use_r1_reg, r1_power, 
                spike_target_hidden, spike_target_output, tau_stdp,
                gamma0, lambda0, nu, epochs,
                n_steps, interpolation, reset_cadc_each_sample
            )

            for epoch in range(epochs):
                print(80*"=")
                print(f"Epoch {epoch+1} of {epochs}")
                t_start_e = time.time()

                fr_train = forward_p(epoch, train_loader, True)
                t_backend_train = fr_train.t_backend
                t_traces = fr_train.t_traces
                t_weight_update = fr_train.t_weight_update

                print(20*"=", "Testing", 20*"=")
                # TEMP DISABLED FOR TESTING
                fr_test = forward_p(epoch, test_loader, False)
                # fr_test = ForwardResult(0., 0., 0., 0., 0.)
                test_loss[epoch] = fr_test.loss
                test_accuracy[epoch] = fr_test.accuracy
                t_backend_test = fr_test.t_backend

                t_epoch = time.time() - t_start_e
                backend_str = f"(Backend: {t_backend_train:.1f} sec training, {t_backend_test:.1f} sec testing)"
                print(f"Time {t_epoch:.1f} sec {backend_str} Traces: {t_traces:.3f}, Weight updates: {t_weight_update:.3f} sec")
                tb.add_scalar("time/epoch", t_epoch, epoch)
            
            tb.flush()

    return RunResult(test_loss, test_accuracy)


def forward(
    backend,
    tb: SummaryWriter,
    tb_options: TbOptions,
    weight_layers: List[np.ndarray],
    m_output: np.ndarray,
    v_output: np.ndarray,
    m_hidden: np.ndarray,
    v_hidden: np.ndarray,
    max_hw_batch_size: int, 
    input_repetitions: int,
    eta: float,
    lr_step_size: int,
    lr_gamma: float,
    w_max: float,
    hw_scale: float,
    input_shift: float,
    sample_separation: float,
    r_big: float,
    r_small: float,
    regularize_per_sample: bool,
    use_r1_reg: bool,
    r1_power: float,
    spike_target_hidden: float,
    spike_target_output: float,
    tau_stdp: float,
    gamma0: float,
    lambda0: float,
    nu: Callable[[int, int], float],
    epochs: int,
    n_steps: int,
    interpolation: int,
    reset_cadc_each_sample: bool,
    epoch: int, 
    data_loader: torch.utils.data.DataLoader, 
    update_weights: bool,
) -> ForwardResult:


    t_backend = 0.
    t_weight_update = 0.
    t_traces = 0.

    dataset_size = len(data_loader.dataset)
    batch_size = data_loader.batch_size
    batches_per_epoch = dataset_size // batch_size
    hw_batch_size = min(batch_size, max_hw_batch_size)
    hw_batch_bounds = np.arange(0, batch_size, hw_batch_size)

    weights_hidden, weights_output = weight_layers
    np.clip(weights_hidden, -w_max, w_max, out=weights_hidden)
    np.clip(weights_output, -w_max, w_max, out=weights_output)

    weight_bins = np.linspace(-hw_scale*w_max*1.01, hw_scale*w_max*1.01, 10*63)
    prob_bins = np.linspace(0., 1., 40)
    tau_upper = sample_separation*1e6
    tau_bins = np.linspace(0., tau_upper, np.int32(np.ceil(tau_upper/r_small)))

    in_ds = np.zeros((dataset_size, n_input))
    y_hat_ds = np.zeros((dataset_size, n_output))
    class_estimate_ds = np.zeros(dataset_size, dtype=int)
    class_ds = np.zeros(dataset_size, dtype=int)
    spikes_per_output_ds = np.zeros((dataset_size, n_output), dtype=int)
    cross_entropy_ds = np.zeros(dataset_size)

    if regularize_per_sample:
        def regularizer_min_spikes(weights, spikes, target):
            sp = spikes >= target  # (batch_size, n_post)
            sp = np.stack(weights.shape[0] * [sp], axis=1)
            return np.where(sp, 0, (-gamma0 * np.abs(weights))[None, ...])
        
        def regularizer_rate(weights, spikes):
            sqr = np.power(spikes, r1_power)  # (batch_size, n_post)
            sqr = np.stack(weights.shape[0] * [sqr], axis=1)
            return lambda0 * weights[None, ...] * sqr
    else:
        def regularizer_min_spikes(weights, spikes, target):
            sp = spikes.mean(axis=0) >= target
            sp = np.vstack(weights.shape[0] * [sp])
            return np.where(sp, 0, -gamma0 * np.abs(weights))            

        def regularizer_rate(weights, spikes):
            # 1/N sum x_i^2, 1/N (sum x_i)^2, (1/N sum x_i)^2 = 1/N^2 (sum x_i)^2
            # return lambda0 * weights * np.power(spikes, r1_power).mean(axis=0)[None, :]
            # return lambda0 * weights * np.power(spikes.sum(axis=0), r1_power)/spikes.shape[0][None, :]
            return lambda0 * weights * np.power(spikes.mean(axis=0), r1_power)[None, :]

    for batch_idx, (batch_x, batch_y) in enumerate(data_loader):

        input_spikes = []
        hidden_spikes = []
        output_spikes = []

        batch_slice = slice(batch_idx*batch_size, (batch_idx+1)*batch_size)
        in_all = in_ds[batch_slice, :]
        y_hat = y_hat_ds[batch_slice, :]
        class_estimate = class_estimate_ds[batch_slice]
        c_all = class_ds[batch_slice]
        y_all = np.zeros((batch_size, n_output))

        traces_hidden = np.zeros((batch_size, n_input, n_hidden))
        traces_output = np.zeros((batch_size, n_hidden, n_output))
        traces_dev = np.zeros((batch_size, 256, 256))
        traces_raw = np.zeros((batch_size, 512, 256))
        traces_hidden_dev = np.zeros((batch_size, input_repetitions, n_input, n_hidden))
        traces_output_dev = np.zeros((batch_size, n_hidden, n_output))
        spikes_per_hidden = np.zeros((batch_size, n_hidden))
        spikes_per_output = spikes_per_output_ds[batch_slice, :]

        labels = np.arange(input_repetitions * n_input) + 256
        # labels = np.hstack(input_repetitions * [np.arange(n_input) + 256])
        batch_x *= 1e-6
        c_all[:] =  batch_y
        y_all[np.arange(batch_size), batch_y] = 1

        for b in range(batch_size):
            times = batch_x[b, :]
            in_all[b, :] = times
            times += input_shift
            times = np.hstack(input_repetitions * [times])
            order = np.argsort(times)
            input_spikes.append(np.vstack([times[order], labels[order]]).T)

        backend.write_weights(*[w*hw_scale for w in weight_layers])

        traces_dev.fill(np.NaN)
        traces_hidden_dev.fill(np.NaN)
        traces_output_dev.fill(np.NaN)
        for s in [slice(i, min(batch_size, i + hw_batch_size)) for i in hw_batch_bounds]:
            # batch_durations = np.zeros((batch_size, 2))
            t_start_b = time.time()
            for _ in range(5):
                spikes, membrane_traces, durations, causal_traces = backend.run(
                        input_spikes[s],
                        n_samples=n_steps // interpolation,
                        record_madc=madc_rec != SampleMADC.off,
                        trigger_reset=reset_cadc_each_sample,)
                # batch_durations[:] = np.array(durations)
                if not (np.array(durations) > 85200).any():
                    # print("Success!")
                    break
                else:
                    print(f"Took too long! {np.max(durations)}")
                    pass
            t_backend += time.time() - t_start_b

            times_hidden = [b_tu[:, 0] - input_shift for b_tu in spikes[0]]
            units_hidden = [b_tu[:, 1].astype(int) for b_tu in spikes[0]]
            times_output = [b_tu[:, 0] - input_shift for b_tu in spikes[1]]
            units_output = [b_tu[:, 1].astype(int) for b_tu in spikes[1]]

            if madc_rec != SampleMADC.off:
                assert backend._madc_samples.size
                fig: plt.figure = plt.figure(figsize=(25, 15), )
                ax = fig.add_subplot(111)
                ax.plot(backend._madc_samples[:, 0], backend._madc_samples[:, 1])
                plt.savefig(f"{madc_rec}.png")

            if measure_hw_correlation:
                assert len(causal_traces) == (s.stop - s.start)
                td = traces_dev[s, ...]
                tr = traces_raw[s, ...]
                thd = traces_hidden_dev[s, ...]
                tod = traces_output_dev[s, ...]
                for b, ct in enumerate(causal_traces):
                    if isinstance(ct, tuple):
                        ct, ct_hidden, ct_output, raw = ct
                        for j in range(input_repetitions):
                            thd[b, j, :, :] = ct_hidden[j*n_input: (j+1)*n_input, :]
                        tod[b, ...] = ct_output
                        tr[b, ...] = raw
                    else:
                        # Backend did not preprocess the correlation array
                        for j in range(input_repetitions):
                            s_first = 2*n_input*j
                            s_last = s_first + 2*n_input
                            for k in range(n_hidden):
                                thd[b, j, :, k, 0] = ct[k*2, s_first:s_last:2]
                                thd[b, j, :, k, 1] = ct[k*2, s_first+1:s_last:2]

                        s_last = 2*(n_hidden - 128)
                        for j in range(n_output):
                            k = 2*(n_hidden + j)
                            tod[b, :128, j, 0] = ct[k, ::2]
                            tod[b, :128, j, 1] = ct[k, 1::2]
                            tod[b, 128:, j, 0] = ct[k+1, :s_last:2]
                            tod[b, 128:, j, 1] = ct[k+1, 1:s_last:2]
                    td[b, :, :] = ct

            if update_weights:
                t_start_trace = time.time()
                compute_traces(
                    in_all[s, :], 
                    units_hidden, times_hidden, units_output, times_output, 
                    s.stop - s.start, tau_stdp,
                    traces_hidden[s, ...], traces_output[s, ...],
                )
                t_traces += time.time() - t_start_trace

            if classifier == Classifier.potential:
                # membrane_traces -> list[layers] -> ndarray.shape = (batches, n_samples, layer_size)
                assert len(membrane_traces) == 2
                membrane_potential_hidden = membrane_traces[0]
                membrane_potential_output = membrane_traces[1]
                assert membrane_potential_hidden.shape == (s.stop - s.start, n_steps // interpolation, n_hidden)
                assert membrane_potential_output.shape == (s.stop - s.start, n_steps // interpolation, n_output)
            else:
                membrane_potential_output = [i for i in range(batch_size)]

            for b in range(s.stop - s.start):
                np.add.at(spikes_per_output[s.start+b, :], units_output[b], 1)
                np.add.at(spikes_per_hidden[s.start+b, :], units_hidden[b], 1)

                hidden_spikes.append(1e6*times_hidden[b])
                output_spikes.append(1e6*times_output[b])

                tau_k = compute_tau(units_output[b], times_output[b], membrane_potential_output[b])
                y_hat[s.start+b, :] = activation_tau(tau_k, nu(epoch, epochs))


        if measure_hw_correlation:
            assert np.all(np.isfinite(traces_dev))
            assert np.all(np.isfinite(traces_hidden_dev))
            assert np.all(np.isfinite(traces_output_dev))
            thd = traces_hidden_dev.mean(axis=1)
            tod = traces_output_dev.copy()
            assert thd.shape == traces_hidden.shape
            assert tod.shape == traces_output.shape

            thf = traces_hidden.flatten()
            thdf = thd.flatten()
            tof = traces_output.flatten()
            todf = tod.flatten()

            th_corr = np.corrcoef(thf, thdf)[0, 1]
            to_corr = np.corrcoef(tof, todf)[0, 1]

            hi = np.argwhere(thf > 1e-1)
            oi = np.argwhere(tof > 1e-1)
            print(f"Non-zero traces, hidden: {hi.size}, output: {oi.size}")
            th_corr_adj = np.corrcoef(thf[hi].flatten(), thdf[hi].flatten())[0, 1] if hi.size else 0.
            to_corr_adj = np.corrcoef(tof[oi].flatten(), todf[oi].flatten())[0, 1] if oi.size else 0.

        # print("BASELINE")
        # for i in range(512):
        #     print("[" + ", ".join(f"{backend.baseline[i, j]:.0f}" for j in range(256)) + "],")

        err = y_hat - y_all  # shape=(batch_size, n_output)

        class_estimate[:] = np.argmax(y_hat, axis=1)
        no_pref = np.logical_and(
            np.isclose(y_hat[:, 0], y_hat[:, 1]), 
            np.isclose(y_hat[:, 0], y_hat[:, 2])
        )
        class_estimate[no_pref] = 3
        n_no_pref = no_pref.sum()
        n_valid = batch_size - n_no_pref

        n_correct = (c_all == class_estimate).sum()
        accuracy = n_correct / batch_size
        adjusted_accuracy = n_correct / max(n_valid, 1)

        # cosine_similarity = np.einsum("bi,bi->b", y_hat, y_all)

        cross_entropy = cross_entropy_ds[batch_slice]
        # Add small constant to y_hat to avoid numerical problems in computing the cross entropy.
        cross_entropy[:] = -np.sum(y_all * np.log(y_hat + np.finfo(y_hat.dtype).eps), axis=1)
        mean_loss = cross_entropy.mean()

        total_spikes_hidden = spikes_per_hidden.sum(axis=1)
        total_spikes_output = spikes_per_output.sum(axis=1)
        where_no_outputs = total_spikes_output == 0
        no_outputs = where_no_outputs.sum()
        
        if no_outputs:
            tsh_no_outputs = total_spikes_hidden[where_no_outputs]
            min_hidden, max_hidden = tsh_no_outputs.min(), tsh_no_outputs.max()
            no_str = f"[No output spikes: {no_outputs}, hidden spikes: {min_hidden} - {max_hidden}]"
        else:
            no_str = ""
        print(f"Batch: {batch_idx+1}/{batches_per_epoch}, Accuracy: {accuracy:.2f} ({adjusted_accuracy:.2f}), Loss: {mean_loss:.3f} {no_str}")

        if update_weights:
            t_start_w = time.time()

            dw_out = (traces_output * err[:, None, :])  # (batch_size, n_hidden, n_output) * (batch_size, n_output) -> (n_hidden, n_output)
            wt = weights_output[None, ...] * traces_output  # shape=(batch_size, n_hidden, n_output)
            bpe = np.einsum("bij,bj->bi", wt, err)  # (batch_size, n_hidden, n_output) (batch_size, n_output) -> (batch_size, n_hidden)
            dw_hidden = (traces_hidden * bpe[:, None, :])  # (batch_size, n_input, n_hidden) (batch_size, n_hidden) -> (n_input, n_hidden)
            
            if regularize_per_sample:
                dw_hidden = np.concatenate(input_repetitions*[dw_hidden], axis=1)
            else:
                dw_out = dw_out.mean(axis=0)
                dw_hidden = dw_hidden.mean(axis=0)
                dw_hidden = np.vstack(input_repetitions*[dw_hidden])

            r0_hidden = regularizer_min_spikes(weights_hidden, spikes_per_hidden, spike_target_hidden)
            r0_output = regularizer_min_spikes(weights_output, spikes_per_output, spike_target_output)
            mean_r0_hidden = r0_hidden.mean()
            mean_r0_output = r0_output.mean()
            dw_hidden += r0_hidden
            dw_out += r0_output
            
            if use_r1_reg:
                r1_hidden = regularizer_rate(weights_hidden, spikes_per_hidden)
                r1_output = regularizer_rate(weights_output, spikes_per_output)
                mean_r1_hidden = r1_hidden.mean()
                mean_r1_output = r1_output.mean()
                dw_hidden += r1_hidden
                dw_out += r1_output

            if regularize_per_sample:
                dw_out = dw_out.mean(axis=0)
                dw_hidden = dw_hidden.mean(axis=0)

            adam_update(
                eta, weights_hidden, m_hidden, v_hidden, dw_hidden, epoch, epochs, lr_step_size, lr_gamma
            )
            eta_hat = adam_update(
                eta, weights_output, m_output, v_output, dw_out, epoch, epochs, lr_step_size, lr_gamma
            )

            t_weight_update += time.time() - t_start_w

            assert np.all(np.isfinite(weights_hidden)), "Non-finite hidden weights"
            assert np.all(np.isfinite(weights_output)), "Non-finite output weights"
            np.clip(weights_hidden, -w_max, w_max, out=weights_hidden)
            np.clip(weights_output, -w_max, w_max, out=weights_output)

            tb_i = epoch * batches_per_epoch + batch_idx

            for cls in range(n_output):
                cls_at = c_all == cls
                cls_accuracy = (class_estimate[cls_at] == cls).sum() / max(cls_at.sum(), 1)
                tb.add_scalar(f"train/class_{data_loader.dataset.class_names[cls]}", cls_accuracy, tb_i)
            tb.add_scalar("train/accuracy", accuracy, tb_i)
            tb.add_scalar("train/accuracy_adjusted", adjusted_accuracy, tb_i)
            tb.add_scalar("train/loss", mean_loss, tb_i)

            if tb_options.log_each_batch:
                hidden_spikes = np.hstack(hidden_spikes)
                output_spikes = np.hstack(output_spikes)

                if measure_hw_correlation:
                    for b in range(batch_size):
                        tb_s = tb_i * batch_size + b
                        tb.add_histogram("per_sample/all", traces_dev[b], tb_s)
                        tb.add_histogram("per_sample/hidden", thd[b], tb_s)
                        tb.add_histogram("per_sample/output", tod[b], tb_s)
                        tb.add_scalar("per_sample/mean_all", traces_dev[b].mean(), tb_s)
                        tb.add_scalar("per_sample/mean_hidden", thd[b].mean(), tb_s)
                        tb.add_scalar("per_sample/mean_output", tod[b].mean(), tb_s)

                    tb.add_scalar("traces/correlation_output", to_corr, tb_i)
                    tb.add_scalar("traces/correlation_hidden", th_corr, tb_i)
                    tb.add_scalar("traces/correlation_adj_output", to_corr_adj, tb_i)
                    tb.add_scalar("traces/correlation_adj_hidden", th_corr_adj, tb_i)
                    tb.add_scalar("traces/hidden_min", traces_hidden_dev.min(), tb_i)
                    tb.add_scalar("traces/hidden_max", traces_hidden_dev.max(), tb_i)
                    tb.add_scalar("traces/output_min", traces_output_dev.min(), tb_i)
                    tb.add_scalar("traces/output_max", traces_output_dev.max(), tb_i)
                    tb.add_histogram("traces/hidden_avg", thd, tb_i,)# bins=np.arange(-32, 32, 1))
                    tb.add_histogram("traces/output_avg", tod, tb_i,)# bins=np.arange(-32, 32, 1))
                    tb.add_histogram("traces/hidden_dev", traces_hidden_dev, tb_i)#, bins=trace_bins)
                    tb.add_histogram("traces/output_dev", traces_output_dev, tb_i)#, bins=trace_bins)
                    tb.add_histogram("traces/baseline", backend.baseline, tb_i, bins=np.arange(256))

                    fig = plt.figure(figsize=(16, 12))
                    gs = GridSpec(1, 1,)# hspace=.01, wspace=.01, top=1, bottom=0, left=0, right=1)
                    ax = fig.add_subplot(gs[0])
                    im = ax.pcolor(backend._routing.weights_assigned)
                    fig.colorbar(im)
                    tb.add_figure(f"weights/assigned", fig, tb_i)
                    
                    fig = plt.figure(figsize=(16, 12))
                    gs = GridSpec(1, 1,)# hspace=.01, wspace=.01, top=1, bottom=0, left=0, right=1)
                    ax = fig.add_subplot(gs[0])
                    im = ax.pcolor(backend.weights_unrolled)
                    fig.colorbar(im)
                    tb.add_figure(f"weights/unrolled", fig, tb_i)

                    fig = plt.figure(figsize=(16, 12))
                    gs = GridSpec(1, 1,)# hspace=.01, wspace=.01, top=1, bottom=0, left=0, right=1)
                    ax = fig.add_subplot(gs[0])
                    im = ax.pcolor(traces_raw.mean(axis=0).T)
                    fig.colorbar(im)
                    tb.add_figure(f"traces/raw", fig, tb_i)

                    fig = plt.figure(figsize=(16, 12))
                    gs = GridSpec(1, 1,)# hspace=.01, wspace=.01, top=1, bottom=0, left=0, right=1)
                    ax = fig.add_subplot(gs[0])
                    im = ax.pcolor(traces_dev.mean(axis=0))
                    fig.colorbar(im)
                    tb.add_figure(f"traces/all", fig, tb_i)

                    fig = plt.figure()
                    gs = GridSpec(1, 1,)# hspace=.01, wspace=.01, top=1, bottom=0, left=0, right=1)
                    ax = fig.add_subplot(gs[0])
                    im = ax.pcolor(thd.mean(axis=0))
                    fig.colorbar(im)
                    tb.add_figure(f"traces/array_hidden", fig, tb_i)

                    fig = plt.figure()
                    gs = GridSpec(1, 1,)# hspace=.01, wspace=.01, top=1, bottom=0, left=0, right=1)
                    ax = fig.add_subplot(gs[0])
                    im = ax.pcolor(tod.mean(axis=0))
                    fig.colorbar(im)
                    tb.add_figure(f"traces/array_output", fig, tb_i)

                    fig = plt.figure()
                    gs = GridSpec(1, 1,)# hspace=.01, wspace=.01, top=1, bottom=0, left=0, right=1)
                    ax = fig.add_subplot(gs[0])
                    im = ax.pcolor(traces_hidden.mean(axis=0))
                    fig.colorbar(im)
                    tb.add_figure(f"traces/array_hidden_offline", fig, tb_i)

                    fig = plt.figure()
                    gs = GridSpec(1, 1,)# hspace=.01, wspace=.01, top=1, bottom=0, left=0, right=1)
                    ax = fig.add_subplot(gs[0])
                    im = ax.pcolor(traces_output.mean(axis=0))
                    fig.colorbar(im)
                    tb.add_figure(f"traces/array_output_offline", fig, tb_i)

                    fig = plt.figure()
                    gs = GridSpec(1, 1,)# hspace=.01, wspace=.01, top=1, bottom=0, left=0, right=1)
                    ax = fig.add_subplot(gs[0])
                    ax.scatter(thdf[hi], thf[hi])
                    tb.add_figure(f"traces/nonzero_hidden", fig, tb_i)

                    fig = plt.figure()
                    gs = GridSpec(1, 1,)# hspace=.01, wspace=.01, top=1, bottom=0, left=0, right=1)
                    ax = fig.add_subplot(gs[0])
                    ax.scatter(todf[oi], tof[oi])
                    tb.add_figure(f"traces/nonzero_output", fig, tb_i)

                    for tn, tcd, tcc in zip(("hidden", "output"), (thd, tod), (traces_hidden, traces_output)):
                        fig = plt.figure()
                        gs = GridSpec(1, 1,)# hspace=.01, wspace=.01, top=1, bottom=0, left=0, right=1)
                        ax = fig.add_subplot(gs[0])
                        ax.scatter(tcd.flatten(), tcc.flatten())
                        tb.add_figure(f"traces/batches_{tn}", fig, tb_i)

                        fig = plt.figure()
                        n = max(int(np.floor(np.sqrt(batch_size))), 1)
                        gs = GridSpec(n, n,)# hspace=.01, wspace=.01, top=1, bottom=0, left=0, right=1)
                        for a in range(n**2):
                            ij = np.unravel_index(a, (n, n))
                            ax: plt.Axes = fig.add_subplot(gs[ij])
                            ax.scatter(tcd[a], tcc[a])
                        tb.add_figure(f"traces/scatter_{tn}", fig, tb_i)

                tb.add_histogram("class/probability", y_hat, tb_i, bins=prob_bins)
                tb.add_histogram("class/cross_entropy", cross_entropy, tb_i)
                tb.add_histogram("class_id/estimate", class_estimate, tb_i)
                tb.add_histogram("class_id/true", c_all, tb_i)
                tb.add_histogram("hidden/input_latency", in_all*1e6, tb_i, bins=tau_bins)
                if hidden_spikes.size:
                    tb.add_histogram("hidden/spike_latency", hidden_spikes, tb_i, bins=tau_bins)
                if output_spikes.size:
                    tb.add_histogram("output/spike_latency", output_spikes, tb_i, bins=tau_bins)
                tb.add_histogram("hidden/spike_counts", spikes_per_hidden, tb_i)
                tb.add_histogram("output/spike_counts", spikes_per_output, tb_i)
                tb.add_histogram("hidden/total_spikes", total_spikes_hidden, tb_i)
                tb.add_histogram("output/total_spikes", total_spikes_output, tb_i)
                tb.add_histogram("hidden/weights", weights_hidden * hw_scale, tb_i, bins=weight_bins)
                tb.add_histogram("output/weights", weights_output * hw_scale, tb_i, bins=weight_bins)

                tb.add_scalar("Learning rate", eta_hat, tb_i)
                tb.add_scalar("reg/spikes_hidden", mean_r0_hidden, tb_i)
                tb.add_scalar("reg/spikes_output", mean_r0_output, tb_i)
                tb.add_histogram("hidden/traces", traces_hidden, tb_i)
                tb.add_histogram("output/traces", traces_output, tb_i)
                tb.add_histogram("hidden/grad", dw_hidden, tb_i)
                tb.add_histogram("output/grad", dw_out, tb_i)
                tb.add_histogram("regularization/spikes_hidden", r0_hidden, tb_i)
                tb.add_histogram("regularization/spikes_output", r0_output, tb_i)
                if use_r1_reg:
                    tb.add_scalar("reg/sqr_rates_hidden", mean_r1_hidden, tb_i)
                    tb.add_scalar("reg/sqr_rates_output", mean_r1_output, tb_i)
                    tb.add_histogram("regularization/sqr_rates_hidden", r1_hidden, tb_i)
                    tb.add_histogram("regularization/sqr_rates_output", r1_output, tb_i)

    t_max = 2e-6*r_big
    assert in_ds.max() <= t_max  and in_ds.min() >= 0., f"{t_max:.2e} {in_ds.max():.3e} {in_ds.min():.3e}"

    n_correct = (class_ds == class_estimate_ds).sum()
    accuracy = n_correct / dataset_size
    mean_loss = cross_entropy_ds.mean()

    if not update_weights:
        for cls in range(n_output):
            cls_at = class_ds == cls
            cls_accuracy = (class_estimate_ds[cls_at] == cls).sum() / cls_at.sum()
            tb.add_scalar(f"test/class_{data_loader.dataset.class_names[cls]}", cls_accuracy, epoch)
        
        no_pref = class_estimate_ds == 3
        n_no_pref = no_pref.sum()
        n_valid = dataset_size - n_no_pref

        adjusted_accuracy = n_correct / max(n_valid, 1)

        tb.add_scalar("test/accuracy", accuracy, epoch)
        tb.add_scalar("test/accuracy_adjusted", adjusted_accuracy, epoch)
        tb.add_scalar("test/loss", mean_loss, epoch)

        if tb_options.log_class_images:
            fig = plt.figure()
            gs = GridSpec(1, 1, left=0, right=1, bottom=0, top=1)
            ax = fig.add_subplot(gs[0])
            ax.set(xticks=(), yticks=())
            fig_t = plt.figure(figsize=(10, 3))
            gs = GridSpec(1, 3, left=0, right=1, bottom=0, top=1, wspace=0)

            # class_estimate_ds = np.argmax(y_hat_ds, axis=1)
            for cls in range(n_output):
                input_cls = in_ds[class_estimate_ds == cls, :]
                ax.scatter(input_cls[:, 0], input_cls[:, 1], color=("r", "g", "b")[cls])

                ax_cls = fig_t.add_subplot(gs[cls])
                ax_cls.set(xticks=(), yticks=())
                ax_cls.scatter(in_ds[:, 0], in_ds[:, 1], color=cm.viridis(y_hat_ds[:, cls]))
            
            tb.add_figure("class_choice", fig_t, epoch)
            tb.add_figure("class_probability", fig, epoch)

        if tb_options.log_noclass_images:
            total_output_spikes = spikes_per_output_ds.sum(axis=1)
            eq_spikes_loc = np.logical_and(
                spikes_per_output_ds[:, 0] == spikes_per_output_ds[:, 1],
                spikes_per_output_ds[:, 1] == spikes_per_output_ds[:, 2]
            )
            multi_eq_loc = np.logical_and(eq_spikes_loc, total_output_spikes > 0)
            
            ax_lim = (-.5e-6, r_big*2e-6 + .5e-6)
            scatter_args = dict(
                xticks=(), yticks=(),
                xlim=ax_lim, ylim=ax_lim,
            )
            if multi_eq_loc.sum():
                fig = plt.figure()
                gs = GridSpec(1, 1)#, left=0, right=1, bottom=0, top=1)
                ax = fig.add_subplot(gs[0])
                ax.set(**scatter_args)
                for cls in range(n_output):
                    input_cls = in_ds[np.logical_and(multi_eq_loc, class_ds == cls), :]
                    ax.scatter(input_cls[:, 0], input_cls[:, 1], color=("r", "g", "b")[cls])
                tb.add_figure("eq/with_spikes", fig, epoch)

            no_spikes_loc = total_output_spikes == 0
            if no_spikes_loc.sum():
                fig = plt.figure()
                gs = GridSpec(1, 1)#, left=0, right=1, bottom=0, top=1)
                ax = fig.add_subplot(gs[0])
                ax.set(**scatter_args)
                for cls in range(n_output):
                    in_class = class_ds == cls
                    no_spikes_cls = np.logical_and(no_spikes_loc, in_class)
                    tb.add_scalar(f"no_spikes/{data_loader.dataset.class_names[cls]}", no_spikes_cls.sum() / in_class.sum(), epoch)
                    input_no_spikes = in_ds[no_spikes_cls, :]
                    ax.scatter(input_no_spikes[:, 0], input_no_spikes[:, 1], color=("r", "g", "b")[cls])
                tb.add_figure("eq/no_spikes", fig, epoch)

    return ForwardResult(mean_loss, accuracy, t_backend, t_traces, t_weight_update)


class StepLR:
    """Decays the learning rate of each parameter group by gamma every
    step_size epochs. Notice that such decay can happen simultaneously with
    other changes to the learning rate from outside this scheduler. When
    last_epoch=-1, sets initial lr as lr.

    Args:
        optimizer (Optimizer): Wrapped optimizer.
        step_size (int): Period of learning rate decay.
        gamma (float): Multiplicative factor of learning rate decay.
            Default: 0.1.
        last_epoch (int): The index of last epoch. Default: -1.
        verbose (bool): If ``True``, prints a message to stdout for
            each update. Default: ``False``.

    Example:
        >>> # Assuming optimizer uses lr = 0.05 for all groups
        >>> # lr = 0.05     if epoch < 30
        >>> # lr = 0.005    if 30 <= epoch < 60
        >>> # lr = 0.0005   if 60 <= epoch < 90
        >>> # ...
        >>> scheduler = StepLR(optimizer, step_size=30, gamma=0.1)
        >>> for epoch in range(100):
        >>>     train(...)
        >>>     validate(...)
        >>>     scheduler.step()
    """

    def __init__(self, step_size, gamma=0.1):
        self.step_size = step_size
        self.gamma = gamma

    def get_lr(self):
        if (self.last_epoch == 0) or (self.last_epoch % self.step_size != 0):
            return [group['lr'] for group in self.optimizer.param_groups]
        return [group['lr'] * self.gamma
                for group in self.optimizer.param_groups]

    def _get_closed_form_lr(self):
        return [base_lr * self.gamma ** (self.last_epoch // self.step_size)
                for base_lr in self.base_lrs]


def adam_update(eta, w, m, v, dw, epoch, epochs, lr_step_size, lr_gamma):
    adam_eps = 1e-8
    adam_beta1 = 0.9   # .1  # .5  # 
    adam_beta2 = 0.999  # .1999 # .9  # 

    # epoch = int(t / dt_update_weights)
    # base_lr * self.gamma ** (self.last_epoch // self.step_size
    eta_hat = eta * lr_gamma**(epoch // lr_step_size)
    # print(f"Epoch: {epoch}, LR-Epoch: {lr_epoch}, eta_hat: {eta_hat:.2e}")
    # lr_epoch = (epoch+1) // lr_step_size
    # eta_hat = eta * 10**(-lr_factor * lr_epoch * (lr_step_size / epochs))
    m *= adam_beta1
    m += (1 - adam_beta1) * dw
    v *= adam_beta2
    v += (1 - adam_beta2) * np.power(dw, 2)
    m_hat = m / (1 - adam_beta1)
    v_hat = v / (1 - adam_beta2)
    w -= eta_hat * m_hat / (np.sqrt(v_hat) + adam_eps)
    return eta_hat


def activation_tau(tau_k, nu):
    """
    tau_k should be normalized such that its values are order-1.
    """

    if np.any(np.isfinite(tau_k)):
        exp_nu_tau = np.exp(-nu * tau_k)
    else:
        exp_nu_tau = np.ones_like(tau_k)
    return exp_nu_tau / np.sum(exp_nu_tau)


@njit(cache=True)
def compute_trace(
        spikes_pre, spikes_post, units_pre, units_post, trace, tau_stdp
):
    """Assumes spikes are sorted from earliest-to-latest"""

    for j, t_post in zip(units_post, spikes_post):
        for i, t_pre in zip(units_pre, spikes_pre):
            delta_t = t_post - t_pre
            if delta_t < 0.:
                break
            trace[i, j] += np.exp(-delta_t/tau_stdp)

    # from itertools import product
    # for i, j in product(range(n_pre), range(n_post)):
    #     spi = np.argwhere(units_pre == i)
    #     spj = np.argwhere(units_post == j)
    #     if spi.size and spj.size:
    #         sti = spikes_pre[spi]
    #         stj = spikes_post[spj]
    #         for t_post in stj:
    #             t_pre = sti[sti < t_post]
    #             trace[i, j] += np.exp(-(t_post - t_pre)/tau_stdp).sum()


def compute_traces(
    all_inputs, 
    units_hidden, times_hidden, units_output, times_output ,
    batch_size, tau_stdp, traces_hidden, traces_output,
):
    all_inputs = all_inputs.copy()
    for b in range(batch_size):
        units_input = np.arange(n_input)
        spike_times_input = all_inputs[b, :]
        times_hidden_b = times_hidden[b].copy()
        units_hidden_b = units_hidden[b].copy()
        times_output_b = times_output[b].copy()
        units_output_b = units_output[b].copy()

        for units, times in (
                (units_input, spike_times_input),
                (units_hidden_b, times_hidden_b), 
                (units_output_b, times_output_b),
            ):
            idx_order = np.argsort(times)
            times[:] = times[idx_order]
            units[:] = units[idx_order]

        compute_trace(
            spike_times_input, times_hidden_b,
            units_input, units_hidden_b,
            traces_hidden[b, ...], tau_stdp
        )
        compute_trace(
            times_hidden_b, times_output_b,
            units_hidden_b, units_output_b,
            traces_output[b, ...], tau_stdp
        )
        # print(f"{b} Hidden trace max: {traces_hidden.max()} non-zero: {np.sum(traces_hidden>0)}, spikes {units_hidden.size}")
        # print(f"{b} Output trace max: {traces_output.max()} non-zero: {np.sum(traces_output>0)}, spikes {units_output.size}")
    return traces_hidden, traces_output


if classifier == Classifier.first_spike:
    def compute_tau(units_output, spikes_output, _) -> np.ndarray:
        tau_k = np.empty(n_output)
        tau_k.fill(np.infty)
        if spikes_output.size:
            tau_k[units_output] = spikes_output
            for o in range(n_output):
                output_o = units_output == o
                if output_o.sum() > 1:
                    if print_multispike_warning:
                        print(f"Output unit {o} produced more than one spike! {units_output} {spikes_output}")
                    tau_k[o] = np.min(spikes_output[output_o])

            tau_k *= 1e6  # Bring usec values into reasonable range
            tau_k -= tau_k.min()  # Ensure softmax operates on values (-inf, 0]
        return tau_k

elif classifier == Classifier.spike_count:
    def compute_tau(units_output, _, __) -> np.ndarray:
        tau_k = np.zeros(n_output)
        np.add.at(tau_k, units_output, 1)
        tau_k -= tau_k.max()
        return tau_k

else:
    def compute_tau(_, __, membrane_potential_output) -> np.ndarray:
        total_v = membrane_potential_output.sum(axis=0)
        total_v -= total_v.max()
        return total_v


# class SummaryWriterHp(SummaryWriter):
#     def add_hparams(
#         self, hparam_dict, metric_dict, hparam_domain_discrete=None, run_name=None
#     ):
#         from torch.utils.tensorboard.summary import hparams

#         torch._C._log_api_usage_once("tensorboard.logging.add_hparams")
#         if type(hparam_dict) is not dict or type(metric_dict) is not dict:
#             raise TypeError('hparam_dict and metric_dict should be dictionary.')
#         exp, ssi, sei = hparams(hparam_dict, metric_dict, hparam_domain_discrete)
        
#         self.file_writer.add_summary(exp)
#         self.file_writer.add_summary(ssi)
#         self.file_writer.add_summary(sei)
#         for k, v in metric_dict.items():
#             self.add_scalar(k, v)



def parse_arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-w", "--wafer", help="The wafer to run on.", type=int, default=69
    )
    parser.add_argument(
        "-f", "--fpga", help="The desired FPGA.", type=int, default=3
    )
    parser.add_argument(
        "-d", "--dir", help="Directory to store results in.", type=Path
    )
    args = parser.parse_args()

    return args


if __name__ == "__main__":
    args = parse_arguments()
    main(args.wafer, args.fpga, args.dir, optimize_hyperparameters)
