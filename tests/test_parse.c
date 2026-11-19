/*
 * test_parse.c - unit tests of parse_uint() (util.c): the strict unsigned decimal parser used for every numeric command-line argument
 * (switch, supervisor, host, swctl, bench). strtol() accepted " 9000" and "+9000"; parse_uint() accepts only the characters '0'-'9'.
 * Prints FAIL for every failed check; exit status 0 only if all passed.
 */
#include "common.h"

#include <limits.h>

static int checks, failures;

#define EXPECT(cond)                                                       \
    do {                                                                   \
        checks++;                                                          \
        if (!(cond)) {                                                     \
            failures++;                                                    \
            printf("  FAIL line %d: %s\n", __LINE__, #cond);               \
        }                                                                  \
    } while (0)

/* The input is good: the value is stored. */
static void good(const char *s, unsigned long lo, unsigned long hi, long want)
{
    long v = -12345;
    int rc = parse_uint(s, lo, hi, &v);

    checks++;
    if (rc != 0 || v != want) {
        failures++;
        printf("  FAIL: parse_uint(\"%s\", %lu, %lu) -> rc %d value %ld, wanted %ld\n", s ? s : "(null)", lo, hi, rc, v, want);
    }
}

/* The input is refused with the given errno, and *out is untouched. */
static void bad(const char *s, unsigned long lo, unsigned long hi, int want_errno)
{
    long v = -12345;

    errno = 0;
    int rc = parse_uint(s, lo, hi, &v);

    checks++;
    if (rc != -1 || v != -12345 || errno != want_errno) {
        failures++;
        printf("  FAIL: parse_uint(\"%s\", %lu, %lu) -> rc %d value %ld errno %d, wanted rc -1, value untouched, errno %d\n", s ? s : "(null)", lo, hi, rc, v, errno, want_errno);
    }
}

int main(void)
{
    printf("parse_uint: TCP port 1-65535\n");
    good("9000", 1, 65535, 9000);
    good("1", 1, 65535, 1);
    good("65535", 1, 65535, 65535);
    good("09000", 1, 65535, 9000);                         /* leading zeros are digits */
    good("0000000000000000000000000000000000009000", 1, 65535, 9000);   /* many leading zeros: not an overflow */
    bad("0", 1, 65535, ERANGE);
    bad("00", 1, 65535, ERANGE);
    bad("65536", 1, 65535, ERANGE);
    bad("+9000", 1, 65535, EINVAL);
    bad("-9000", 1, 65535, EINVAL);
    bad("-0", 1, 65535, EINVAL);
    bad("+0", 0, 65535, EINVAL);
    bad(" 9000", 1, 65535, EINVAL);
    bad("9000 ", 1, 65535, EINVAL);
    bad("\t9000", 1, 65535, EINVAL);
    bad("9000\n", 1, 65535, EINVAL);
    bad("9 000", 1, 65535, EINVAL);
    bad("abc", 1, 65535, EINVAL);
    bad("9000abc", 1, 65535, EINVAL);
    bad("abc9000", 1, 65535, EINVAL);
    bad("90.0", 1, 65535, EINVAL);
    bad("9000.", 1, 65535, EINVAL);
    bad("0x2328", 1, 65535, EINVAL);
    bad("9e3", 1, 65535, EINVAL);
    bad("\xd9\xa9", 1, 65535, EINVAL);                     /* a non-ASCII digit */
    bad("", 1, 65535, EINVAL);
    bad(NULL, 1, 65535, EINVAL);

    printf("parse_uint: overflow\n");
    bad("99999999999999999999", 1, 65535, ERANGE);         /* > ULONG_MAX */
    bad("18446744073709551616", 0, LONG_MAX, ERANGE);      /* ULONG_MAX + 1 */
    bad("18446744073709551615", 0, 65535, ERANGE);         /* ULONG_MAX itself: fits, but out of range */
    bad("9223372036854775808", 0, LONG_MAX, ERANGE);       /* LONG_MAX + 1 */
    good("9223372036854775807", 0, LONG_MAX, LONG_MAX);    /* LONG_MAX */
    bad("99999999999999999999999999999999999999999999", 0, LONG_MAX, ERANGE);
    bad("4294967296", 1, 65535, ERANGE);                   /* 2^32: must not wrap to 0 */
    bad("4295032831", 1, 65535, ERANGE);                   /* 2^32 + 65535: must not wrap to 65535 */
    bad("18446744073709551617", 0, 65535, ERANGE);         /* wraps to 1 in 64-bit arithmetic: must be refused */

    printf("parse_uint: number of ports 1-64\n");
    good("1", 1, 64, 1);
    good("64", 1, 64, 64);
    bad("0", 1, 64, ERANGE);
    bad("65", 1, 64, ERANGE);
    bad("+4", 1, 64, EINVAL);
    bad("-4", 1, 64, EINVAL);
    bad(" 4", 1, 64, EINVAL);
    bad("4 ", 1, 64, EINVAL);
    bad("4x", 1, 64, EINVAL);
    bad("", 1, 64, EINVAL);

    printf("parse_uint: other ranges (0 allowed, vlan, priority, flood count)\n");
    good("0", 0, 1000000000L, 0);
    good("1000000000", 0, 1000000000L, 1000000000L);
    bad("1000000001", 0, 1000000000L, ERANGE);
    good("4094", 1, 4094, 4094);
    bad("4095", 1, 4094, ERANGE);
    good("2", 0, 2, 2);
    bad("3", 0, 2, ERANGE);
    bad("-1", 0, 2, EINVAL);

    printf("parse_uint: an impossible range (hi above LONG_MAX) is refused, not wrapped\n");
    bad("5", 0, (unsigned long)LONG_MAX + 1, EINVAL);

    printf("%d checks, %d failures\n", checks, failures);
    return failures ? EXIT_FAILURE : EXIT_SUCCESS;
}
