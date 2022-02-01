nextflow.enable.dsl=2

include { process_flux_cal } from './process_flux_cal'
include { process_pol_cal } from './process_pol_cal'
include { process_frb } from './process_frb'

params.cpasspoly_fluxcal = 5
params.cpasspoly_polcal = 5
params.cpasspoly_frbcal = 5

workflow {
    flux_cal_solns = process_flux_cal(
        "${params.label}_fluxcal",
        params.label,
        params.data_fluxcal,
        params.fcm,
        params.ra_fluxcal,
        params.dec_fluxcal,
        params.cpasspoly_fluxcal
    )
    pol_cal_solns = process_pol_cal(
        "${params.label}_polcal",
        params.label,
        params.data_polcal,
        params.fcm,
        params.ra_polcal,
        params.dec_polcal,
        params.cpasspoly_polcal,
        flux_cal_solns,
        params.num_ints_polcal,
        params.int_len_polcal,
        params.offset_polcal,
        params.dm_polcal,
        params.centre_freq_polcal
    )
    process_frb(
        params.label,
        params.data_frb,
        params.snoopy,
        params.fcm,
        params.ra0,
        params.dec0,
        flux_cal_solns,
        pol_cal_solns,
        params.cpasspoly_frb,
        params.num_ints_frb,
        params.int_len_frb,
        params.offset_frb,
        params.dm_frb,
        params.centre_freq_frb
    )
}