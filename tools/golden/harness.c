/*
 * Golden-vector harness for gpsview's formatting code.
 *
 * gen_golden.sh pastes the real gpsd / cgps functions into this file (between
 * the BEGIN/END markers in the generated copy), compiles it, and runs it.  The
 * output is one TSV line per case, so the Python tests can check the port
 * against the C output byte for byte.
 */
#include <math.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define UNUSED
#define strlcpy(d, s, n) snprintf((d), (n), "%s", (s))
#define FEET_TO_METERS 0.3048
#define METERS_TO_FEET (1 / FEET_TO_METERS)
#define MPS_TO_MPH (1 / 0.44704)
#define MPS_TO_KNOTS (3600.0 / 1852.0)
#define MPS_TO_KPH 3.6

enum deg_str_type { deg_dd, deg_ddmm, deg_ddmmss };

/* cgps keeps these as globals; ecef_to_str() reads them. */
static float altfactor = METERS_TO_FEET;
static char *altunits = "ft";

/* @@BEGIN_EXTRACTED@@ */
/* @@END_EXTRACTED@@ */

static void out_deg(double f, enum deg_str_type t, const char *sp, const char *sn)
{
    char buf[64];
    deg_to_str2(t, f, buf, sizeof(buf), sp, sn);
    printf("deg_to_str2\t%d\t%.17g\t%s\t%s\t%s\n", (int)t, f,
           sp ? sp : "-", sn ? sn : "-", buf);
}

static void out_mh(double lat, double lon)
{
    printf("maidenhead\t%.17g\t%.17g\t%s\n", lat, lon, maidenhead(lat, lon));
}

static void out_ep(double ep, double factor, const char *units)
{
    printf("ep_to_str\t%.17g\t%.17g\t%s\t%s\n", ep, factor, units,
           ep_to_str(ep, factor, (char *)units));
}

static void out_dop(double d)
{
    printf("dop_to_str\t%.17g\t%s\n", d, dop_to_str(d));
}

static void out_tenth(double v, double mn, double mx)
{
    printf("tenth_to_str\t%.17g\t%.17g\t%.17g\t%s\n", v, mn, mx,
           tenth_to_str(v, mn, mx));
}

static void out_int(int v, int mn, int mx)
{
    printf("int_to_str\t%d\t%d\t%d\t%s\n", v, mn, mx, int_to_str(v, mn, mx));
}

static void out_ecef(double p, double v)
{
    printf("ecef_to_str\t%.17g\t%.17g\t%s\t%s\n", p, v, altunits,
           ecef_to_str(p, v));
}

int main(void)
{
    const double degs[] = {0.0, -0.0, 45.123456789, -122.9876543, 0.00000001,
                           -0.00000001, 89.99999999, -179.5, 359.9999999,
                           360.0, -360.0, 361.0, 0.5 / 3600, 12.0 + 34.0 / 60 + 56.789 / 3600,
                           NAN, INFINITY};
    const enum deg_str_type types[] = {deg_dd, deg_ddmm, deg_ddmmss};
    const double mh[][2] = {{0, 0}, {37.7749, -122.4194}, {-33.8688, 151.2093},
                            {89.99999, 179.99999}, {-89.5, -179.9}, {51.5, -0.12},
                            {-22.9, -43.2}, {35.6812, 139.7671}, {90.5, 0}, {0, 181}};
    const double eps[] = {0.0, 1.5, 150.0, 99.9999, 100.0, -3.2, 12345.6, NAN, INFINITY};
    const double dops[] = {0.0, 1.23, 12.3456, 99.999, 123.456, NAN, INFINITY};
    const double tenths[] = {12.34, -5.5, 0.0, 359.0, 400.0, -91.0, NAN, INFINITY};
    const double ecefs_p[] = {6378137.12345, -2700000.5, 0.0, NAN, INFINITY};
    const double ecefs_v[] = {1.5, -0.0001, NAN, 2.5};

    for (size_t i = 0; i < sizeof(degs) / sizeof(degs[0]); i++) {
        for (size_t t = 0; t < 3; t++) {
            out_deg(degs[i], types[t], NULL, NULL);
            out_deg(degs[i], types[t], " N", " S");
            out_deg(degs[i], types[t], " E", " W");
        }
    }
    for (size_t i = 0; i < sizeof(mh) / sizeof(mh[0]); i++) {
        out_mh(mh[i][0], mh[i][1]);
    }
    for (size_t i = 0; i < sizeof(eps) / sizeof(eps[0]); i++) {
        out_ep(eps[i], METERS_TO_FEET, "ft");
        out_ep(eps[i], 1, "m");
        out_ep(eps[i], MPS_TO_KPH, "km/h");
        out_ep(eps[i], MPS_TO_KNOTS, "knots");
        out_ep(eps[i], 1, "deg");
    }
    for (size_t i = 0; i < sizeof(dops) / sizeof(dops[0]); i++) {
        out_dop(dops[i]);
    }
    for (size_t i = 0; i < sizeof(tenths) / sizeof(tenths[0]); i++) {
        out_tenth(tenths[i], -90.0, 90.0);
        out_tenth(tenths[i], 0.0, 359.0);
        out_tenth(tenths[i], 0.0, 254.0);
    }
    out_int(5, 1, 462);
    out_int(0, 1, 462);
    out_int(462, 1, 462);
    out_int(463, 1, 462);
    out_int(-1, 0, 500);
    out_int(500, 0, 500);
    for (int units = 0; units < 2; units++) {
        if (units == 1) {
            altfactor = 1;
            altunits = "m";
        }
        for (size_t i = 0; i < sizeof(ecefs_p) / sizeof(ecefs_p[0]); i++) {
            for (size_t j = 0; j < sizeof(ecefs_v) / sizeof(ecefs_v[0]); j++) {
                out_ecef(ecefs_p[i], ecefs_v[j]);
            }
        }
    }
    return 0;
}
