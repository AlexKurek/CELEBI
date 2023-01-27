from argparse import ArgumentDefaultsHelpFormatter, ArgumentParser

from astropy.coordinates import SkyCoord as sc
from astroquery.utils.tap.core import TapPlus


def _main():
    parser = ArgumentParser(
        description="Lookup source position in RACS",
        formatter_class=ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("-o", required=True, help="Output RACS positions file")
    parser.add_argument(
        "-a", required=True, help="Output ASKAP positions file"
    )
    parser.add_argument("-n", required=True, help="Output names file")
    parser.add_argument("-r", required=True, help="Output region file")
    parser.add_argument("files", nargs="+", help="Source stats files")
    args = parser.parse_args()

    casdatap = TapPlus(url="https://casda.csiro.au/casda_vo_tools/tap")
    askap_file = open(args.a, "a")
    pos_file = open(args.o, "a")
    name_file = open(args.n, "a")
    region_file = open(args.r, "a")

    names = []

    for f in args.files:
        print(f)
        coord = Coord(f)

        table = RACS_lookup(coord.ra_hms, coord.dec_dms, casdatap)

        if len(table) == 0:
            continue

        print(table)

        # sort table by peak flux and use the brightest entry
        table.sort("flux_peak")
        brightest = table[-1]

        # highest precision position is with the deg coords
        brightest_sc = sc(
            brightest["ra_deg_cont"], brightest["dec_deg_cont"], unit="deg"
        )
        ra_hms, dec_dms = brightest_sc.to_string("hmsdms").split()

        # Avoid repeating sources and sources with low S/N
        if brightest["component_name"] not in names and coord.sn >= 7:
            names.append(brightest["component_name"])

            askap_file.write(
                writestr(
                    coord.ra_hms, coord.ra_err, coord.dec_dms, coord.dec_err
                )
            )
            pos_file.write(
                writestr(
                    ra_hms, max(float(brightest["ra_err"]), 0.01), dec_dms, max(float(brightest["dec_err"]), 0.01)
                )
            )
            name_file.write(brightest["component_name"] + "\n")
            region_str = f'{coord.ra_hms}, {coord.dec_dms}, {coord.ra_err}", {coord.dec_err}", 0'
            region_file.write(
                f'fk5;ellipse({region_str.replace("h", ":").replace("d", ":").replace("m", ":").replace("s", ":")}) # text="{brightest["component_name"]} - S/N={coord.sn:.2f}"\n'
            )

    askap_file.close()
    pos_file.close()
    name_file.close()


class Coord:
    def __init__(self, stats):
        fields = {}
        f = open(stats)
        for line in f:
            line = line[:-1].split(": ")  # [:-1] to trim newline
            fields[line[0]] = line[1].strip()  # strip extra whitespace
        f.close()

        self.ra_hms = fields["Actual RA"]  # hms
        self.ra_err = max(float(fields["Est. RA error (mas)"]) / 1e3, 0.01)  # arcseconds
        self.dec_dms = fields["Actual Dec"]  # dms
        self.dec_err = max(float(fields["Est. Dec error (mas)"]) / 1e3, 0.01) # arcseconds
        self.sn = float(fields["S/N"])


def RACS_lookup(ra_hms, dec_dms, casdatap):
    c = sc(ra_hms, dec_dms, unit="hour,deg")
    # search radius of ~10 arcseconds
    print(f"Looking up {c}")
    job = casdatap.launch_job_async(
        f"SELECT * FROM casda.continuum_component where 1=CONTAINS(POINT('ICRS', ra_deg_cont, dec_deg_cont),CIRCLE('ICRS',{c.ra.value},{c.dec.value},0.005)) and project_id = 23"
    )
    return job.get_results()


def writestr(ra_hms, ra_err, dec_dms, dec_err):
    return f"{ra_hms},{ra_err},{dec_dms},{dec_err}\n"


if __name__ == "__main__":
    _main()