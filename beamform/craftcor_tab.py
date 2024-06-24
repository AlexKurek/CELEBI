"""
Tied-array beamforming vcraft files, based on "craftcor.py".

Copyright (C) CSIRO 2017
"""
import glob
import logging
import os
from re import X
import signal
import sys

from timeit import default_timer as timer

import numpy as np
import vcraft
from argparse import ArgumentDefaultsHelpFormatter, ArgumentParser

from calc11 import ResultsFile
from joblib import Parallel, delayed, parallel_backend
from scipy.interpolate import interp1d
from parse_aips import aipscor
from scipy.fft import next_fast_len

# antenna name to index mapping
ant_map = {}
for i in range(36):
    ant_map[f"ak{i+1:02d}"] = i

__author__ = ["Keith Bannister <keith.bannister@csiro.au>",
              "Danica Scott <danica.r.scott@postgrad.curtin.edu.au>"]




def next_biggest_fftlen(x, bw):
    """

    NOTE: IMPORTANT!!!! - on the unlikely event that this breaks, i.e. the size of outputted data products for
    each antenna mismatches, it might be caused by precision error, in which case, one solution would be to 
    truncate the buffer sizes to the smallest antenna buffer size in thw [sum.py] script that adds the antenna 
    together.

    Very brute force approach, find the next largest length of samples within a single channel
    that makes the full 336MHz buffer a 5-smooth number (optimised for FFT).

    Parameters
    ----------
    x : current number of samples

    Returns
    -------
    ysamp : number of samples
    y : number of fine channels
    yguard : truncated num of samples on 1 side
    """

    xguard = int(5 * x // 64)
    x -= 2*xguard
    y = x

    while True:
        if y*bw == next_fast_len(y*bw):

            yguard = int(xguard * (float(y)/float(x)))

            return y + 2*yguard, y, yguard
    
        y+=1
    
def next_smallest_fftlen(x, bw):
    """

    Very brute force approach, find the next largest length of samples within a single channel
    that makes the full 336MHz buffer a 5-smooth number (optimised for FFT).

    Parameters
    ----------
    x : current number of samples

    Returns
    -------
    ysamp : number of samples
    y : number of fine channels
    yguard : truncated num of samples on 1 side
    """

    xguard = int(5 * x // 64)
    x -= 2*xguard
    y = x

    while True:
        if y*bw == next_fast_len(y*bw):

            yguard = int(xguard * (float(y)/float(x)))

            return y + 2*yguard, y, yguard
    
        y-=1



def parse_snoopy(snoopy_file: str) -> "list[str]":
    """Parse snoopy file, returning a candidate as a list of strings.

    We expect this file to only contain one candidate (the triggering
    candidate), so we only return one line.

    :param snoopy_file: Path to snoopy candidate file
    :type snoopy_file: str
    :return: Candidate information as a list of strings. Each string is
        a whitespace-separated value in the candidate file.
    :rtype: list[str]
    """
    nocommentlines = []
    for line in open(snoopy_file):
        print(line)
        if len(line) > 1 and not line[0] == "#":
            nocommentlines.append(line)
            print(f"Snoopy info {nocommentlines}")
    if len(nocommentlines) != 1:
        print("ERROR: No information found")
        sys.exit()

    return nocommentlines[0].split()


def _main():
    values = parse_args()

    if values.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    # Start by getting the antenna list from the calc file
    calcresults = ResultsFile(values.calcfile)

    # get vcraft files from data directory
    vcraftfiles = []
    for anname in calcresults.telnames:
        antennadir = values.data + '/' + anname
        if not os.path.exists(antennadir):
            print("antenna directory " + antennadir + " doesn't exist, aborting")
            sys.exit()

        beamdirs = sorted(glob.glob(antennadir + "/*"))
        # FIXME: this doesn't take into account busted downloads when only one polarisation is present. More info should be provided upstream
        if values.pol == "X":
            vcraftfiles += sorted(glob.glob(beamdirs[0] + "/*[ac]*vcraft"))
        elif values.pol == "Y":
            vcraftfiles += sorted(glob.glob(beamdirs[1] + "/*[ac]*vcraft"))
        else:
            print(f"{values.pol} is not a valid polarisation! Must be x or y")
            sys.exit(1)

    start = timer()
    # FIXME: This should be replaced by robustifying the ResultsFile and getting the data from it, rather than using this separate parsing on the calc file
    sources = load_sources(values.calcfile)
    print(f"load_sources: {timer()-start} s")

    # hacking delays
    start = timer()
    # FIXME: I'm sure this is not robust against changing the antenna subset, but is only relevant for very old FRBs with hardware delays
    delaymap = parse_delays(values)
    antennas = [
        AntennaSource(mux)
        for mux in vcraft.mux_by_antenna(vcraftfiles, delaymap)
    ]
    print(f"Parse antennas: {timer()-start} s")
    print(("NUMBER OF ANTENNAS TO BE BEAMFORMED", len(antennas)))

    given_offset = values.offset
    start = timer()
    # FIXME: Could replace the antenna list here by the calcresults object, or even better, by the values.an antenna that we actually want to load
    corr = Correlator(antennas, sources, values, abs_delay=given_offset)
    print(f"setup Correlator: {timer()-start} s")

    try:
        start = timer()
        if values.ics:
            print("PERFORMING INCOHERENT SUM")
            ant_ics = corr.do_ics(values.an)
            fn = f"{values.outfile}_{values.pol}_{values.an:02d}"
            print(f"saving {fn}")
            np.save(fn, ant_ics)
        else:
            print("PERFORMING TIED-ARRAY BEAMFORMING")

            mjd, DM = None, None
            if values.snoopy is not None:
                cand = parse_snoopy(values.snoopy)
                mjd = float(cand[7])

            temp = corr.do_tab(values.an, mjd, values.DM)
            fn = values.outfile
            print(f"saving output to {fn} with size {temp.shape}")
            np.save(fn, temp)

    finally:
        print(f"beamforming: {timer() - start}")
        print("done")


class AntennaSource:
    def __init__(self, vfile):
        self.vfile = vfile
        self.antname = self.vfile.hdr["ANT"][0].lower()
        self.antno = int(self.vfile.hdr["ANTENNA_NO"][0])
        self.mjdstart = self.vfile.start_mjd
        self.trigger_frame = self.vfile.start_frameid
        self.hdr = self.vfile.hdr
        self.init_geom_delay_us = None
        self.all_geom_delays = []
        self.all_mjds = []
        self.pol = self.vfile.pol.lower()
        print(f"antenna {self.antname} {self.vfile.freqconfig}")

    def do_f_tab(self, corr, iant, mjd, DM):
        # iant is the number of the antenna in the AIPS AN table, minus 1 (i.e., a zero based index from the 36 antennas)
        start = timer()
        self.frparams = FringeRotParams(corr, self)
        # calculate sample start
        framediff_samp = corr.refant.trigger_frame - self.trigger_frame
        geom_delay_us, geom_delay_rate_us = corr.get_geometric_delay_delayrate_us(
            self)

        self.all_geom_delays.append(geom_delay_us)
        self.all_mjds.append(corr.curr_mjd_mid)

        fixed_delay_us = corr.get_fixed_delay_usec(self.antno)
        total_delay_samp = framediff_samp
        whole_delay = int(np.round(total_delay_samp))


        #############################################################
        # Where the loading of the data happens
        #
        #############################################################


        nfine = corr.nfft - 2 * corr.nguard_chan

        nsamp = corr.nint * corr.nfft
        nchan_coarse = len(corr.freqs)

        # time-dependent geometric delays
        # np.linspace(0, 1, nsamp) == time in units of integrations
        geom_delays_us = (
            geom_delay_us
            + geom_delay_rate_us * np.linspace(0, 1, nsamp, dtype = np.float32)
            - fixed_delay_us
        )
        # np.save("TEMP_geom_delays_us.npy", geom_delays_us)
        # del geom_delays_us
        # geom_delays_us = np.load("TEMP_geom_delays_us.npy", mmap_mode="r")

        sampoff = whole_delay + corr.abs_delay

        # update sampoff and nsamp based on mjd and DM for better cropping
        if (mjd is not None) and (DM is not None):
            # perform rough pulse cropping
            start_samp = int(self.mjdstart * 8.64e10 * 32/27)
            cand_samp = int(mjd * 8.64e10 * 32/27)
            print(f"Cand MJD from start of buffer (in samples): {cand_samp - start_samp}")
            pulse_offset_samp = cand_samp - start_samp

            # measure DM sweep
            kDM = 4149.377593
            DM_sweep_samp = int(abs(kDM * DM * 1e6 * (1/(min(corr.freqs)**2) - 1/(max(corr.freqs)**2))) * 32/27)

            # calculate crop bounds
            crop_offset = pulse_offset_samp - int(DM_sweep_samp * 1.1) # 1.1 is extra buffer length
            crop_nsamp = (int(DM_sweep_samp * 1.2) // nchan_coarse) * nchan_coarse

            # get next biggest buffer length for FFT
            crop_nsamp, _, _ = next_biggest_fftlen(crop_nsamp, corr.ncoarse_chan)

            crop_start = crop_offset
            crop_end = crop_offset + crop_nsamp

            # check bounds of new crop
            buffer_end = sampoff + nsamp  # END BOUNDS OF ORIGINAL CROP
            buffer_start = sampoff

            print(f"Initial crop start: {crop_start}")
            print(f"Initial Crop end: {crop_end}")
            print("------")
            print(f"Buffer start: {buffer_start}")
            print(f"Buffer end: {buffer_end}")

            if crop_start > buffer_start:
                sampoff = crop_start

            # check width of new crop
            if crop_end > buffer_end:
                # gone past end, change width
                nsamp = buffer_end - sampoff
            
            else:
                nsamp = crop_end - sampoff

            print("------")
            print(f"New crop start: {sampoff}")
            print(f"New crop end: {sampoff + nsamp}")
            print(f"New crop nguard: {int(5 * nsamp // 64)}")


            # get next smallest buffer length for FFT, if nothing has changed, this will return the same as next_biggest_fftlen,
            # if nsamp changes, next_smallest_fftlen will ensure the optimal width is within the original buffer bounds
            nsamp, nfine, corr.nguard_chan = next_smallest_fftlen(nsamp, corr.ncoarse_chan)
            # corr.nguard_chan = int(5 * nsamp// 64)
            # nfine = nsamp - 2 * corr.nguard_chan
            corr.fine_chanbw = 1.0/float(nfine)

            print("------")
            print(f"FFT crop start: {sampoff}")
            print(f"FFT crop end: {sampoff + nsamp}")
            print(f"FFT crop nguard: {corr.nguard_chan}")

            print("------")
            print(f"geom delays crop start: {sampoff - buffer_start}")
            print(f"geom delays crop end: {sampoff - buffer_start + nsamp}")

            # crop geom delays
            geom_delays_us = geom_delays_us[sampoff - buffer_start:sampoff - buffer_start + nsamp]

            print("\n#######################################################")
            print(f"Changed crop based on a DM sweep of {DM_sweep_samp} us pulse MJD offset of {pulse_offset_samp} us")
            print(f"Allowing a DM_sweep padding of 1.1x{DM_sweep_samp} = {int(1.1*DM_sweep_samp)} us")
            print("----------")
            print(f"Buffer start: {buffer_start}, Buffer end: {buffer_end}, Buffer width: {buffer_end - buffer_start}")
            print(f"Crop start: {sampoff}, Crop end: {sampoff + nsamp}, Crop width: {nsamp}")
            print("#######################################################\n")

            # save crop MJD to txt file
            crop_MJD = self.mjdstart + ((sampoff - buffer_start) * 27/32)/8.64e10
            with open("frb_crop_MJD.txt", "w") as file:
                file.write(f"{crop_MJD}")


        else:
            # We will only get to this part if the polarisation calibrator is being beamformed.
            # In this case, we don't care about careful cropping, so we will always take the full crop, unless
            # that crop is larger than 3.8s, in which case we will take a crop of 3.8s. Doesn't matter where the crop starts
            # since the generate_dynspec script will crop the sides anyway, so we will always get a good integer number
            # of pulses for polcal.
            old_nsamp = nsamp
            # 11851850
            if nsamp >= 3800000:
                nsamp = 3800000

                corr.nguard_chan = int(5 * nsamp // 64)

                # crop geom delays
                geom_delays_us = geom_delays_us[:nsamp]
                nfine = nsamp - 2 * corr.nguard_chan
                corr.fine_chanbw = 1.0/float(nfine)

            print("\n#######################################################")
            print(f"Old nsamp: {old_nsamp}, New nsamp: {nsamp}")
            print("#######################################################\n")


        print(f"# fine channels {nfine}")
        print(f"# coarse channels {nchan_coarse}")
        print(f"# oversample guard (2x) {corr.nguard_chan}")
        data_out = np.zeros(
            (corr.nint, nfine * nchan_coarse, corr.npol_in), dtype=np.complex64
        )


        print(("Zero-based, full-array antenna #: ", iant, self.antname))
        frameid = self.vfile.start_frameid + sampoff
        print(
            "FRAMEID: "
            + str(frameid)
            + ", remainder from 32: "
            + str(frameid % 32)
        )
        # To avoid iPFB fractional delay, set FRAMEID such that the
        # remainder is 0

        print("samp_start + nsamp <= self.nsamps")
        print(f"{sampoff} + {nsamp} <= {self.vfile.nsamps}")
        print("corr.sideband is ", corr.sideband)
        rawd = self.vfile.read(sampoff, nsamp)
        # np.save("TEMP_rawd.npy", rawd)
        # # np.save("NEW_rawd.npy", rawd)
        # print(f"rawd shape: {rawd.shape}")
        # del rawd
        # rawd = np.load("TEMP_rawd.npy", mmap_mode="r")


        # save corrected MJD to txt file
        corrected_MJD = self.mjdstart + (geom_delay_us - fixed_delay_us)/(1e6*24*3600)
        with open("corrected_ant_MJD.txt", "a") as file:
            file.write(f"{self.antname}:    {corrected_MJD}\n")

        # np.save("NEW_geoms.npy", geom_delays_us)

        assert rawd.shape == (
            nsamp,
            corr.ncoarse_chan,
        ), f"Unexpected shape from vfile: {rawd.shape} expected ({nsamp},{corr.ncoarse_chan})"

        # Fine channel frequencies from -0.5 MHz to 0.5 MHz
        freqs = (
            np.arange(nfine, dtype = np.float32) - float(nfine) / 2.0
        ) * corr.fine_chanbw

        print(freqs)

        # Leave the fine channel frequencies running the same direction regardless of sideband - we will convert the sideband sense prior to correction
        freqs = -freqs
        #if corr.sideband == -1:
        #    freqs = -freqs

        # Save and reload to improve memory efficiency
        # np.save("TEMP_freqs.npy", freqs)
        # del freqs
        # freqs = np.load("TEMP_freqs.npy", mmap_mode="r")
        start = timer()

        def process_chan(c):
            # Channel frequency
            cfreq = corr.freqs[c]

            # rawd's shape: (nsamp, corr.ncoarse_chan)
            x1 = rawd[:, c].reshape(-1, nsamp)

            # Conjugate the incoming data if needed to turn it into LSB
            if corr.sideband == 1: # FIXME this is a change
                x1 = np.conj(x1)

            # Fringe rotation for Earth's rotation - use opposite LO sign for lower sideband data
            #turn_fringe = -cfreq * geom_delays_us * corr.sideband #FIXME now everything is LSB this should be unnecessary
            turn_fringe = cfreq * geom_delays_us

            phasor_fringe = np.exp(
                np.pi * 2j * turn_fringe, dtype=np.complex64
            )

            x1 = x1 * phasor_fringe

            # xfguard is xf1 with the ends trimmed off
            xf1 = np.fft.fft(x1, axis=1)
            xf1 = np.fft.fftshift(xf1, axes=1)
            
            # If it is upper sideband, need to conjugate and reverse the channel order to make it appear lower sideband
            # FIXME: not needed if data is already turned into LSB in the time domain
            #if corr.sideband == 1:
            #    xf1 = np.conj(xf1)
            #    xf1 = np.flip(xf1, axis=1)

            xfguard_f = xf1[
                :, corr.nguard_chan: corr.nguard_chan + nfine:
            ]  # scale because oterhwise it overflows

            # Fractional sample phases
            turn_frac = freqs * np.mean(geom_delays_us)

            # phasors to rotate the data with constant amplitude = 1
            phasor = np.exp(np.pi * 2j * turn_frac, dtype=np.complex64)

            # get absolute frequencies in gigahertz
            freq_ghz = (cfreq + freqs) / 1e3

            # apply calibration solutions to phasor
            # print(f"Aips SOlution?: {corr.aips.get_solution(iant, 0, freq_ghz)}")
            phasor /= corr.aips.get_solution(iant, 0, freq_ghz)

            xfguard_f = xfguard_f * phasor

            # select the channels for this coarse channel
            fcstart = c * nfine
            fcend = (c + 1) * nfine
            data_out[:, fcstart:fcend, 0] = xfguard_f

            # print(f"{corr.nguard_chan}, {nfine}")
            print(f"{c}:{fcstart}:{fcend}")

        Parallel(n_jobs=corr.values.cpus, require="sharedmem")(
            delayed(process_chan)(c)
            for c in range(corr.ncoarse_chan)
        )

        with open("fftlen", "w") as f:
            f.write(f"{nsamp}")

        print(f"do_f_tab (stage 2): {timer()-start} s")

        if np.isnan(data_out).any():
            print("WARNING: Output contains NaNs. Calibration solutions not available?")

        print(f"data out shape: {data_out.shape}")
        print(data_out)
        print(np.mean(data_out))
        return data_out

    def do_ics(self, corr, an):
        nfine = corr.nfft - 2 * corr.nguard_chan

        # number of 1 ms-time resolution samples
        nsamp = nfine//1000
        nchan = corr.ncoarse_chan

        start_mjd = corr.curr_mjd_start
        dt_mjd = 1/(24*60*60*1000)
        end_mjd = start_mjd + nsamp*dt_mjd

        t_mjd = np.linspace(start_mjd, end_mjd, nsamp)
        np.save(f"t_mjd.npy", t_mjd)

        ics_data = np.zeros((nchan, nsamp))

        total_delay_samp = corr.refant.trigger_frame - self.trigger_frame
        whole_delay = int(np.round(total_delay_samp))
        rawd = self.vfile.read(whole_delay + corr.abs_delay, corr.nfft)
        np.save("TEMP_rawd.npy", rawd)
        del rawd
        rawd = np.load("TEMP_rawd.npy", mmap_mode="r")

        def process_chan(c):
            print(f"{self.antname}\t{c:03d}/{nchan}")
            cfreq = corr.freqs[c]
            x1 = rawd[:, c].reshape(-1, corr.nfft)
            xf1 = np.fft.fft(x1, axis=1)
            xf1 = np.fft.fftshift(xf1, axes=1)
            xfguard_f = xf1[
                0, corr.nguard_chan: corr.nguard_chan + nfine:
            ]

            XX_1us = np.abs(np.fft.ifft(xfguard_f))**2

            # tscrunch
            for t in range(nsamp):
                ics_data[c, t] = np.sum(XX_1us[t*1000:(t+1)*1000])

        Parallel(n_jobs=corr.values.cpus, require="sharedmem")(
            delayed(process_chan)(c)
            for c in range(nchan)
        )

        return ics_data


class FringeRotParams:
    cols = ("U (m)", "V (m)", "W (m)", "DELAY (us)")

    def __init__(self, corr, ant):
        print("frdata_mid keys:", corr.frdata_mid.keys())
        mid_data = corr.frdata_mid[ant.antname]
        self.u, self.v, self.w, self.delay = list(
            map(float, [mid_data[c] for c in FringeRotParams.cols])
        )
        self.delay_start = float(corr.frdata_start[ant.antname]["DELAY (us)"])
        self.delay_end = float(corr.frdata_end[ant.antname]["DELAY (us)"])
        self.delay_rate = (self.delay_end - self.delay_start) / float(
            corr.nint
        )
        self.ant = ant
        self.corr = corr

    def __str__(self):
        s = f"FR {self.ant.antname} uvw=({self.u},{self.v},{self.w}) m = {self.delay} us"
        return s

    __repr__ = __str__


class Correlator:
    def __init__(self, ants, sources, values, abs_delay=0):
        self.running = True
        signal.signal(signal.SIGINT, self.exit_gracefully)
        signal.signal(signal.SIGTERM, self.exit_gracefully)
        self.ants = ants
        self.values = values

        self.parse_parset()

        for ia, a in enumerate(self.ants):
            a.ia = ia
            a.antpos = self.get_ant_location(a.antno)

        refantname = self.parset[
            "cp.ingest.tasks.FringeRotationTask.params.refant"
        ].lower()
        self.abs_delay = abs_delay

        # Set reference antenna to be one with latest trigger_frame so
        # we always have a positive first sample index
        trigger_frames = [a.trigger_frame for a in self.ants]
        self.refant = ants[np.argmax(trigger_frames)]

        # Determine nfft and nguard_chan such that we always have the
        # maximal number of samples.
        # First get maximum sample offset (assumes no given offset)
        trigger_offsets = [
            int(np.round(self.refant.trigger_frame - a.trigger_frame))
            for a in self.ants
        ]

        sample_offsets = [
            max(a.vfile.sample_offsets) for a in self.ants
        ]

        # number of samples in final spectra
        nsamp = int(self.refant.vfile.nsamps
                    - max(trigger_offsets)
                    - max(sample_offsets))

        self.nfft = nsamp
        self.nguard_chan = int(5 * nsamp // 64)

        # with open("fftlen", "w") as f:
        #     f.write(f"{nsamp}")

        print(f"nfft = {self.nfft}")
        print(f"nguard_chan = {self.nguard_chan}")

        # old way: user specified
        # self.nfft = 64 * values.fft_size
        # self.nguard_chan = 5 * values.fft_size

        self.calcresults = ResultsFile(values.calcfile)
        self.dutc = 0
        self.mjd0 = self.refant.mjdstart + self.dutc / 86400.0
        self.frame0 = self.refant.trigger_frame
        self.nint = values.nint
        self.oversamp = 32.0 / 27.0
        self.fs = self.oversamp  # samples per microsecnd
        self.ncoarse_chan = len(self.refant.vfile.freqs)
        self.sideband = 1 if values.uppersideband else -1
        self.coarse_chanbw = 1.0
        self.nfine_per_coarse = self.nfft - 2 * self.nguard_chan
        self.nfine_chan = self.ncoarse_chan * self.nfine_per_coarse
        self.fine_chanbw = self.coarse_chanbw / float(self.nfine_per_coarse)
        self.full_bw = self.fine_chanbw * self.nfine_chan
        self.fscrunch = values.fscrunch
        assert self.fscrunch >= 1
        assert (
            self.nfine_per_coarse % self.fscrunch == 0
        ), "Fsrunch must yield an integer number of fine channels per coarse channel"
        self.nfine_out_per_coarse = self.nfine_per_coarse / self.fscrunch
        self.nfine_out_chan = self.nfine_out_per_coarse * self.ncoarse_chan
        self.out_chanbw = self.coarse_chanbw / float(self.nfine_out_per_coarse)
        self.npol_in = 1
        self.npol_out = 1
        self.f0 = self.ants[0].vfile.freqs[0]
        self.freqs = self.ants[0].vfile.freqs
        self.fmid = self.freqs.mean()
        self.inttime_secs = float(self.nint * self.nfft) / (self.fs * 1e6)
        self.inttime_days = self.inttime_secs / 86400.0
        self.curr_intno = 0
        self.curr_samp = self.curr_intno * self.nint + 1000
        self.calcmjd()
        self.get_fr_data()
        self.pol = self.ants[0].pol
        if not values.ics:
            self.parse_aips_calibration()

        logging.debug(
            "F0 %f FINE CHANNEL %f kHz num=%d freqs=%s",
            self.f0,
            self.fine_chanbw * 1e3,
            self.nfine_chan,
            self.freqs,
        )

    def exit_gracefully(self, signum, frame):
        self.running = False

    def parse_parset(self):
        self.parset = {}

        # open the fcm file
        with open(self.values.parset) as f:
            for line in f:
                if "=" not in line or line.startswith("#"):
                    continue

                name, value = line.strip().split("=")
                name = name.strip()
                value = value.strip()
                self.parset[name] = value

    def parse_aips_calibration(self):
        self.aips = AipsGainSolutions(
            self.ants,
            self.values,
            self.values.aips_c,
            self.pol,
            self.freqs,
        )

    def get_ant_location(self, antno):
        key = f"common.antenna.ant{antno}.location.itrf"
        value = self.parset[key]
        location = list(
            map(float, value.replace("[", "").replace("]", "").split(","))
        )
        return location

    def get_fixed_delay_usec(self, antno):
        key = f"common.antenna.ant{antno}.delay"
        value = self.parset[key]
        delayns = float(value.replace("ns", ""))
        delayus = delayns / 1e3

        return delayus

    def get_geometric_delay_delayrate_us(self, ant):
        fr1 = FringeRotParams(self, ant)
        fr2 = FringeRotParams(self, self.refant)

        # TODO: There is a discrepancy here, below comment says fr1 is ref ant,
        # but above suggests fr2 is?

        # fr1: reference antenna
        # Account for effects of Earth's rotation
        # delay = fr1.delay - fr2.delay
        # delayrate = fr1.delay_rate - fr2.delay_rate
        delay = fr1.delay_start - fr2.delay_start
        delayrate = fr1.delay_rate - fr2.delay_rate

        with open(f"delays/{ant.antno}_ant_delays.dat", "w") as f:
            f.write(f"#field fr1({ant}) fr2({self.refant})\n")
            f.write(f"delay_start {fr1.delay_start} {fr2.delay_start}\n")
            f.write(f"delay {fr1.delay} {fr2.delay}\n")
            f.write(f"delay_end {fr1.delay_end} {fr2.delay_end}\n")
            f.write(f"delay_rate {fr1.delay_rate} {fr2.delay_rate}\n")

        return (delay, delayrate)

    def calcmjd(self):
        i = float(self.curr_intno)
        abs_delay_days = float(self.abs_delay) / 86400.0 / (self.fs * 1e6)
        self.curr_mjd_start = (
            self.mjd0 + self.inttime_days * (i + 0.0) + abs_delay_days
        )
        self.curr_mjd_mid = (
            self.mjd0 + self.inttime_days * (i + 0.5) + abs_delay_days
        )
        self.curr_mjd_end = (
            self.mjd0 + self.inttime_days * (i + 1.0) + abs_delay_days
        )

    def get_calc_results(self, mjd):
        res = self.calcresults.scans[0].eval_src0_poly(mjd)

        return res

    def get_fr_data(self):
        self.frdata_start = self.get_calc_results(self.curr_mjd_start)
        self.frdata_mid = self.get_calc_results(self.curr_mjd_mid)
        self.frdata_end = self.get_calc_results(self.curr_mjd_end)

    def do_tab(self, an=None, mjd = None, DM = None):
        # Tied-array beamforming

        nsamp = self.nint
        nchan = self.ncoarse_chan * self.nfine_per_coarse

        # print(nsamp, nchan, self.npol_in)
        # sum_aligned = np.zeros(
        #     (nsamp, nchan, self.npol_in), dtype=np.complex64
        # )

        start = timer()
        print("## Operate on only antenna #: " + str(an))
        ant = self.ants[an]
        iant = ant_map[ant.antname]
        temp = ant.do_f_tab(self, iant, mjd, DM)
        print(f"do_f_tab (total): {timer()-start} s")
        return temp

    def do_ics(self, an):
        # Incoherent sum
        ant = self.ants[an]
        ant_ics = ant.do_ics(self, an)

        return ant_ics


class AipsGainSolutions:
    # FIXME: Should provide the three file names separately, and selfcal should be optional
    def __init__(
        self, ants, values, bp_c_root=None, pol=None, freqs=None
    ):
        """Loads AIPS exported bandpass, delay, and gain selfcal solutions.
        Expects 3 files whose root matches that of the bp file given

        """
        print("Using AIPS bandpass solutions")
        nant = None
        nfreq = None
        with open(bp_c_root) as fl:
            for line in fl:
                if "NAXIS2" in line:
                    nant = int(line.split()[2])
                    print(f"nant = {nant}")
                if "TFDIM11" in line:
                    nfreq = int(line.split()[2])
        if nant is None or nfreq is None:
            print(
                "WARNING! nant or nfreq not assigned while parsing AIPS bandpass"
            )
        print(f"nant = {nant}")
        fmax = freqs[0] + 0.5  # in MHz
        bw = len(freqs)  # in MHz
        self.freqs = (
            -np.arange(float(nfreq)) / nfreq * bw
            + fmax
            - float(bw) / nfreq / 2
        ) / 1e3  # reassign freqs in GHz
        self.bp_real = np.full(
            (nfreq, 36), np.nan, dtype=np.complex64
        )
        self.bp_imag = np.full(
            (nfreq, 36), np.nan, dtype=np.complex64
        )
        g_real = np.full((1, 36), np.nan, dtype=np.complex64)
        g_imag = np.full((1, 36), np.nan, dtype=np.complex64)

        # look for a README and get fring, selfcal filenames
        drcal = os.path.dirname(bp_c_root)

        readme = glob.glob("README*")
        if len(readme) == 1:
            with open(readme[0]) as fl:
                for line in fl:
                    if "delays" in line and ".sn.txt" in line:
                        fring_f = line.split()[0]
                    if "selfcal" in line and ".sn.txt" in line:
                        sc_f = line.split()[0]
                        print(f"FOUND SELFCAL FILE: {sc_f}")
        else:
            print("No or multiple readme file exists for AIPS")
            sys.exit(1)

        aips_cor = aipscor(fring_f, sc_f, bp_c_root)
        if values.an == None:
            loadantennas = range(36)
        else:
            antname = ants[values.an].antname
            iant = ant_map[antname]
            loadantennas = [iant]
        for iant in loadantennas:
            print(f"Loading antenna {iant} from {loadantennas}")
            bp = aips_cor.get_phase_bandpass(iant, pol)
            print("Phase bandpass loaded")
            bp = np.fliplr([bp])[0]  # decreasing order

            # fring delay
            delta_t_fring_ns = (
                aips_cor.get_delay_fring(iant, pol) * 1e9
            )
            print("Delay FRING loaded")
            phases = delta_t_fring_ns * self.freqs
            phases -= phases[
                int(len(phases) / 2)
            ]  # TODO! READ THE REFERENCE FREQUENCY AND SET TO THAT REFERENCE

            bp *= np.exp(np.pi * 2j * phases, dtype=np.complex64)

            try:
                g = aips_cor.get_phase_fring(
                    iant, pol
                ) * aips_cor.get_phase_selfcal(iant, pol)
                g = 1 / g  # inverse of gain
                print("Phases loaded")
            except Exception as e:
                print(e)
                g = 0
            bp = np.conj(bp)
            self.bp_real[:, iant] = np.real(bp)
            self.bp_imag[:, iant] = np.imag(bp)
            g_real[0, iant] = np.real(g)
            g_imag[0, iant] = np.imag(g)
        print("Finished loading bandpasses")

        self.bp_real_interp = [
            interp1d(
                self.freqs,
                self.bp_real[:, iant],
                fill_value=(
                    self.bp_real[0, iant],
                    self.bp_real[-1, iant],
                ),
                bounds_error=False,
            )
            for iant in range(36)
        ]
        self.bp_imag_interp = [
            interp1d(
                self.freqs,
                self.bp_imag[:, iant],
                fill_value=(
                    self.bp_imag[0, iant],
                    self.bp_imag[-1, iant],
                ),
                bounds_error=False,
            )
            for iant in range(36)
        ]
        self.bp_coeff = None

        self.g_real = g_real
        self.g_imag = g_imag
        print("Finished AIPS solutions init")

    def get_solution(self, iant, time, freq_ghz):
        """
        Get solution including time and bandpass
        iant - antenna index (zero-based from the full array of 36 antennas, not the subset for this observation)
        time - some version of time. Ignored for now
        freq_ghz - frequency float in Ghz
        """
        if self.bp_real is None:
            # no bandpass/gain solution was passed
            bp_value = np.array([1])
        elif self.bp_coeff is not None:  # Use AIPS polyfit coefficient
            bp_fit = np.poly1d(self.bp_coeff[iant, 0, :]) + 1j * np.poly1d(
                self.bp_coeff[iant, 1, :]
            )
            bp_value = bp_fit(freq_ghz * 1e3)
        else:
            # AIPS polyfit coefficient doesn't exist. Use Miriad/AIPS
            # bandpass interpolation
            f_real = self.bp_real_interp[iant](freq_ghz)
            f_imag = self.bp_imag_interp[iant](freq_ghz)
            bp_value = f_real + 1j * f_imag

        g_value = self.g_real[0, iant] + 1j * self.g_imag[0, iant]
        total_value = bp_value * g_value

        return total_value


def parse_args():
    parser = ArgumentParser(
        description="Perform polyphase filterbank inversion and tied-"
        "array beamforming to produce a fine spectrum from vcraft "
        "voltages",
        formatter_class=ArgumentDefaultsHelpFormatter,
    )

    # need to add in snoopy file for the rough MDJ time of the burst for better cropping
    parser.add_argument("--snoopy", default = None, help = "Snoopy file with rough pulse MJD")
    parser.add_argument("--DM", help = "DM of FRB", type = float)

    parser.add_argument(
        "-d",
        "--data",
        type=str,
        required=True,
        help="Directory containing data to process",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        dest="verbose",
        action="store_true",
        help="Be verbose",
        default=False,
    )
    parser.add_argument(
        "-o", "--outfile", help="Output fits/.npy file", default="corr.fits"
    )
    parser.add_argument(
        "-n",
        "--fft-size",
        type=int,
        help="Multiple of 64 channels to make channels- default=1",
        default=1,
    )
    parser.add_argument("--calcfile", help="Calc file for fringe rotation")
    parser.add_argument("-w", "--hwfile", help="Hw delay file")
    parser.add_argument("-p", "--parset", help="Parset for delays")
    parser.add_argument(
        "-i",
        "--nint",
        help="Number of fine spectra to average",
        type=int,
        default=128,
    )
    parser.add_argument(
        "-f",
        "--fscrunch",
        help="Frequency average by this factor",
        default=1,
        type=int,
    )
    parser.add_argument(
        "--aips_c", help="AIPS banpass polynomial fit coeffs", default=None
    )
    parser.add_argument(
        "--an", type=int, help="Specific antenna", default=None
    )
    parser.add_argument(
        "--offset", type=int, help="FFT offset to add", default=0
    )
    parser.add_argument(
        "--pol", type=str, help="Polarisation to process (x or y)"
    )
    parser.add_argument(
        "--cpus",
        type=int,
        default=1,
        help="Number of CPUs to parallelise across (Default = 1)"
    )
    parser.add_argument(
        "--ics",
        action="store_true",
        default=False,
        help="Incoherent sum mode. Produces intensity dynamic spectrum at "
             " 1 ms time resolution."
    )
    parser.add_argument(
        "--uppersideband",
        action="store_true",
        default=False,
        help="Set when the data is from the ASKAP high band where the "
             "frequency axis is not reversed."
    )

    return parser.parse_args()


def load_sources(calcfile):
    calc_input = calcfile.replace(".im", ".calc")
    d = {}
    for line in open(calc_input):
        if len(line) == 0 or line.startswith("#"):
            continue
        bits = line.split(":")
        if len(bits) != 2:
            continue

        k, v = bits

        d[k.strip()] = v.strip()

    assert d["NUM SOURCES"] == "1"
    name = d["SOURCE 0 NAME"]

    # ra/dec in radians
    ra = float(d["SOURCE 0 RA"]) * 180/np.pi
    dec = float(d["SOURCE 0 DEC"]) * 180/np.pi
    print(f"load_sources: {ra} {dec}")

    sources = [{"name": name, "ra": ra, "dec": dec}]

    return sources


def parse_delays(values):
    delayfile = values.calcfile.replace(".im", ".hwdelays")
    if os.path.exists(delayfile) == False:
        delayfile = values.hwfile
    # print(delayfile)
    delays = {}
    if delayfile is not None and os.path.exists(delayfile):
        with open(delayfile) as dfile:
            for line in dfile:
                bits = line.split()
                if not line.startswith("#") and len(bits) == 2:
                    raw = -int(bits[1])
                    if raw % 8 != 0:  # if it is not a multiple of 8, round
                        new = int(8 * round(float(raw) / 8))
                        print(("hwdelay ", raw, " rounded to ", new))
                        delays[bits[0].strip()] = new
                    else:
                        delays[bits[0].strip()] = raw

        logging.info("Loaded %s delays from %s", len(delays), delayfile)
    else:
        logging.info("No delays loaded. %s does not exist", delayfile)

    return delays


if __name__ == "__main__":
    _main()
