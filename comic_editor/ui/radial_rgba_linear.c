/* Owned optional RGBA linear sampler, ABI1.
 * No interpolation grid/weights/order changes, no FMA or fast-math.
 * MSVC x64 evaluates independent double channel pairs with SSE2; other
 * builds retain scalar volatile checkpoints. Each channel keeps its exact
 * y-then-x products, sequential corner sums and float32 output rounding.
 * Inputs are finite native float32 premultiplied RGBA with row-major storage;
 * coordinates are finite native doubles on the unchanged global source grid.
 * Source/coordinate/output storage is exclusively caller-owned for the call.
 * Unknown/native-unsupported cases must be handled by original SciPy in Python.
 * Source arithmetic derived from pinned SciPy1.18.0 order1/grid-constant,
 * but this is separately written code, not a replacement SciPy distribution.
 */
#include <stdint.h>
#include <stddef.h>
#include <float.h>

#if defined(_WIN32)
#define RGBA_API __declspec(dllexport)
#define RGBA_CALL __cdecl
#else
#define RGBA_API
#define RGBA_CALL
#endif

#define RGBA_ABI UINT64_C(0x5247424142490001)
#define RGBA_MAX_POINTS UINT64_C(9216)
#define RGBA_COORD_LIMIT 4503599627370496.0

RGBA_API uint64_t RGBA_CALL radial_rgba_abi(void)
{
    union { float f; uint32_t u; } single;
    union { double d; uint64_t u; } wide;
    single.f = 1.0f;
    wide.d = 1.0;
    if (sizeof(void*) != 8 || sizeof(float) != 4 || sizeof(double) != 8
            || FLT_RADIX != 2 || FLT_MANT_DIG != 24 || DBL_MANT_DIG != 53
            || single.u != UINT32_C(0x3f800000)
            || wide.u != UINT64_C(0x3ff0000000000000))
        return 0;
    return RGBA_ABI;
}

static int64_t rgba_floor(double coordinate)
{
    /* Finite abs(coordinate)<2**52 precondition makes this conversion safe.
     * This equals floor even for negative fractional values, without libm.
     */
    int64_t integer = (int64_t)coordinate;
    if ((double)integer > coordinate)
        --integer;
    return integer;
}

/* Explicit expansion preserves channel-outer source reads and output stores.
 * Each CORNER keeps all original volatile rounding checkpoints.
 * Transparent corners use positive float zeros; no invalid pointer is formed.
 */
static const float rgba_transparent[4] = {0.0f, 0.0f, 0.0f, 0.0f};
#define RGBA_CORNER(PTR, WY, WX, CHANNEL) do { \
    double coefficient = (double)(PTR)[CHANNEL]; \
    volatile double after_y = coefficient * (WY); \
    volatile double after_x = after_y * (WX); \
    volatile double next_total = total + after_x; \
    total = next_total; \
} while (0)
#define RGBA_CHANNEL(CHANNEL) do { \
    volatile double total = 0.0; \
    RGBA_CORNER(p00, weight_y0, weight_x0, CHANNEL); \
    RGBA_CORNER(p01, weight_y0, weight_x1, CHANNEL); \
    RGBA_CORNER(p10, weight_y1, weight_x0, CHANNEL); \
    RGBA_CORNER(p11, weight_y1, weight_x1, CHANNEL); \
    out[CHANNEL] = (float)total; \
} while (0)

#if defined(_MSC_VER) && defined(_M_X64)
#include <emmintrin.h>
static __m128d rgba_pair(const float *corner, int high) {
    __m128 values = _mm_loadu_ps(corner);
    if (high) values = _mm_movehl_ps(values, values);
    return _mm_cvtps_pd(values);
}
static __m128d rgba_pair_corner(__m128d total, const float *corner,
                               double wy, double wx, int high) {
    __m128d after_y = _mm_mul_pd(rgba_pair(corner, high), _mm_set1_pd(wy));
    __m128d after_x = _mm_mul_pd(after_y, _mm_set1_pd(wx));
    return _mm_add_pd(total, after_x);
}
static void rgba_pairs(const float *p00, const float *p01,
                       const float *p10, const float *p11,
                       double y0, double y1, double x0, double x1, float *out) {
    int high;
    for (high = 0; high < 2; ++high) {
        __m128d total = _mm_setzero_pd();
        total = rgba_pair_corner(total, p00, y0, x0, high);
        total = rgba_pair_corner(total, p01, y0, x1, high);
        total = rgba_pair_corner(total, p10, y1, x0, high);
        total = rgba_pair_corner(total, p11, y1, x1, high);
        _mm_storel_pi((__m64 *)(out + high * 2), _mm_cvtpd_ps(total));
    }
}
#endif

RGBA_API int RGBA_CALL radial_rgba_linear(
    const float *source, uint64_t height, uint64_t width,
    const double *y_coordinates, const double *x_coordinates,
    uint64_t points, float *output)
{
    uint64_t point;
    if (radial_rgba_abi() != RGBA_ABI || source == NULL || output == NULL
            || y_coordinates == NULL || x_coordinates == NULL
            || height == 0 || width == 0
            || height > UINT64_C(2147483647) || width > UINT64_C(2147483647)
            || points == 0 || points > RGBA_MAX_POINTS
            || height > ((uint64_t)SIZE_MAX / sizeof(float) / 4) / width)
        return 1;
    /* Reject the complete coordinate batch before writing any output. NaN and
     * infinity also fail these ordered finite bounds, without libm helpers.
     */
    for (point = 0; point < points; ++point) {
        double y = y_coordinates[point], x = x_coordinates[point];
        if (!(y > -RGBA_COORD_LIMIT && y < RGBA_COORD_LIMIT
                && x > -RGBA_COORD_LIMIT && x < RGBA_COORD_LIMIT))
            return 2;
    }
    for (point = 0; point < points; ++point) {
        double y = y_coordinates[point], x = x_coordinates[point];
        int64_t floor_y = rgba_floor(y), floor_x = rgba_floor(x);
        /* Volatile checkpoints prevent contraction/regrouping/excess-register
         * precision. Arithmetic order is y then x and 00,01,10,11 per channel.
         * Second weights must be1-first, not the original fractional part.
         */
        volatile double delta_y = y - (double)floor_y;
        volatile double delta_x = x - (double)floor_x;
        volatile double first_y = 1.0 - delta_y;
        volatile double first_x = 1.0 - delta_x;
        volatile double second_y = 1.0 - first_y;
        volatile double second_x = 1.0 - first_x;
        double weight_y0 = first_y, weight_y1 = second_y;
        double weight_x0 = first_x, weight_x1 = second_x;
        int64_t row0 = floor_y, row1 = floor_y + 1;
        int64_t column0 = floor_x, column1 = floor_x + 1;
        int valid_row0 = row0 >= 0 && row0 < (int64_t)height;
        int valid_row1 = row1 >= 0 && row1 < (int64_t)height;
        int valid_column0 = column0 >= 0 && column0 < (int64_t)width;
        int valid_column1 = column1 >= 0 && column1 < (int64_t)width;
        const float *p00 = valid_row0 && valid_column0
            ? source + ((uint64_t)row0 * width + (uint64_t)column0) * 4 : rgba_transparent;
        const float *p01 = valid_row0 && valid_column1
            ? source + ((uint64_t)row0 * width + (uint64_t)column1) * 4 : rgba_transparent;
        const float *p10 = valid_row1 && valid_column0
            ? source + ((uint64_t)row1 * width + (uint64_t)column0) * 4 : rgba_transparent;
        const float *p11 = valid_row1 && valid_column1
            ? source + ((uint64_t)row1 * width + (uint64_t)column1) * 4 : rgba_transparent;
        float *out = output + point * 4;
#if defined(_MSC_VER) && defined(_M_X64)
        /* Independent channel pairs use the same rounded double y/x products
         * and sequential 00,01,10,11 sums. No FMA, grid or sample changes.
         * Conversion rounds each channel to float32 before Python accumulation.
         */
        rgba_pairs(p00,p01,p10,p11,weight_y0,weight_y1,weight_x0,weight_x1,out);
#else
        RGBA_CHANNEL(0);
        RGBA_CHANNEL(1);
        RGBA_CHANNEL(2);
        RGBA_CHANNEL(3);
#endif
    }
    return 0;
}

#undef RGBA_CHANNEL
#undef RGBA_CORNER
