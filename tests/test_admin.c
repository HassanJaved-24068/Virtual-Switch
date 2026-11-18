/*
 * test_admin.c - unit tests of the admin command parser (Part 15): admin_parse() in src/admin.c. No sockets, no switch.
 * Valid forms of the eight commands, every kind of bad input (numbers, ranges, missing / extra words, odd characters, length limit), the text of the
 * reasons that swctl shows, and that a small error buffer is never overrun.
 */
#include "admin.h"

#include "common.h"

#include <ctype.h>

static int checks = 0, failures = 0;

#define EXPECT(cond)                                                       \
    do {                                                                   \
        checks++;                                                          \
        if (!(cond)) {                                                     \
            failures++;                                                    \
            printf("  FAIL line %d: %s\n", __LINE__, #cond);               \
        }                                                                  \
    } while (0)

#define NP 4

static admin_cmd_t C;
static char E[128];

static int parse(const char *line)
{
    memset(&C, 0xAB, sizeof C);
    E[0] = '\0';
    return admin_parse(line, NP, &C, E, sizeof E);
}

static bool bad(const char *line, const char *reason_part)
{
    int rc = parse(line);
    return rc == -1 && strstr(E, reason_part) != NULL;
}

static void test_valid(void)
{
    printf("parse: the eight commands\n");
    EXPECT(parse("show mac") == 0 && C.kind == CMD_SHOW_MAC);
    EXPECT(parse("show ports") == 0 && C.kind == CMD_SHOW_PORTS);
    EXPECT(parse("clear mac") == 0 && C.kind == CMD_CLEAR_MAC);
    EXPECT(parse("shutdown 3") == 0 && C.kind == CMD_SHUTDOWN && C.port == 3);
    EXPECT(parse("up 1") == 0 && C.kind == CMD_UP && C.port == 1);
    EXPECT(parse("vlan 2 10") == 0 && C.kind == CMD_VLAN && C.port == 2 && C.arg == 10);
    EXPECT(parse("mirror 1 4") == 0 && C.kind == CMD_MIRROR && C.port == 1 && C.arg == 4);
    EXPECT(parse("mirror 3 off") == 0 && C.kind == CMD_MIRROR_OFF && C.port == 3);

    printf("parse: spaces and tabs between words, edge values\n");
    EXPECT(parse("  show \t mac   ") == 0 && C.kind == CMD_SHOW_MAC);
    EXPECT(parse("vlan\t4\t4094") == 0 && C.port == 4 && C.arg == 4094);          /* highest port, highest VLAN */
    EXPECT(parse("vlan 1 1") == 0 && C.port == 1 && C.arg == 1);                  /* lowest */
    EXPECT(parse("shutdown 4") == 0 && C.port == 4);
    EXPECT(parse("shutdown 04") == 0 && C.port == 4);                             /* digits only: leading zeros are still a number */
    EXPECT(parse("mirror 4 1") == 0 && C.port == 4 && C.arg == 1);
    EXPECT(admin_parse("up 1", 1, &C, E, sizeof E) == 0 && C.port == 1);          /* a one-port switch */
    EXPECT(admin_parse("up 2", 1, &C, E, sizeof E) == -1);
}

static void test_invalid(void)
{
    printf("parse: invalid commands are refused with a reason\n");
    EXPECT(bad("", "empty"));
    EXPECT(bad("   \t ", "empty"));
    EXPECT(bad("bogus", "unknown command 'bogus'"));
    EXPECT(bad("SHOW MAC", "unknown command"));                                   /* case sensitive */
    EXPECT(bad("show", "usage"));
    EXPECT(bad("show foo", "unknown show target"));
    EXPECT(bad("show mac extra", "too many"));
    EXPECT(bad("show macs", "unknown show target"));
    EXPECT(bad("clear", "usage"));
    EXPECT(bad("clear ports", "unknown clear target"));
    EXPECT(bad("clear mac now", "usage"));
    EXPECT(bad("stats", "unknown command"));                                      /* not a command of the socket */
    EXPECT(bad("stats --watch", "unknown command"));

    printf("parse: port numbers\n");
    EXPECT(bad("shutdown", "usage"));
    EXPECT(bad("shutdown 1 2", "usage"));
    EXPECT(bad("shutdown 0", "port 0 out of range (1-4)"));
    EXPECT(bad("shutdown 5", "port 5 out of range (1-4)"));
    EXPECT(bad("up 65", "out of range"));
    EXPECT(bad("shutdown -1", "bad port number"));
    EXPECT(bad("shutdown +1", "bad port number"));
    EXPECT(bad("shutdown 1x", "bad port number"));
    EXPECT(bad("shutdown x", "bad port number"));
    EXPECT(bad("shutdown 0x1", "bad port number"));
    EXPECT(bad("shutdown 1.5", "bad port number"));
    EXPECT(bad("shutdown 1234567", "bad port number"));                           /* more than six digits */
    EXPECT(bad("shutdown 99999999999999999999", "bad port number"));              /* would overflow a long */

    printf("parse: VLANs\n");
    EXPECT(bad("vlan", "usage"));
    EXPECT(bad("vlan 1", "usage"));
    EXPECT(bad("vlan 1 2 3", "usage"));
    EXPECT(bad("vlan 1 0", "VLAN 0 out of range (1-4094)"));
    EXPECT(bad("vlan 1 4095", "VLAN 4095 out of range (1-4094)"));
    EXPECT(bad("vlan 1 65535", "out of range"));
    EXPECT(bad("vlan 1 abc", "bad VLAN"));
    EXPECT(bad("vlan 1 -5", "bad VLAN"));
    EXPECT(bad("vlan 9 10", "port 9 out of range"));                              /* the port is checked first */
    EXPECT(bad("vlan x 10", "bad port number"));

    printf("parse: mirror\n");
    EXPECT(bad("mirror", "usage"));
    EXPECT(bad("mirror 1", "usage"));
    EXPECT(bad("mirror 1 2 3", "usage"));
    EXPECT(bad("mirror 1 1", "cannot mirror to itself"));
    EXPECT(bad("mirror 1 9", "port 9 out of range"));
    EXPECT(bad("mirror 9 1", "port 9 out of range"));
    EXPECT(bad("mirror 1 0", "port 0 out of range"));
    EXPECT(bad("mirror 1 OFF", "bad port number"));
    EXPECT(bad("mirror 1 of", "bad port number"));
    EXPECT(bad("mirror 1 off now", "usage"));
}

static void test_characters_and_length(void)
{
    printf("parse: control characters and the length limit\n");
    EXPECT(bad("show\x01mac", "bad character"));
    EXPECT(bad("show mac\x7f", "bad character"));
    EXPECT(bad("show\r\nmac", "bad character"));                                  /* swctl never sends it; the switch strips one trailing \r itself */
    EXPECT(bad("show \xc3\xa9", "bad character"));                                /* not ASCII */

    char line[400];
    memset(line, 'a', sizeof line - 1);
    line[sizeof line - 1] = '\0';
    EXPECT(bad(line, "too long"));
    /* exactly ADMIN_MAX_LINE bytes is accepted by the length check (and refused as an unknown command); one more is "too long" */
    memset(line, 'a', ADMIN_MAX_LINE);
    line[ADMIN_MAX_LINE] = '\0';
    EXPECT(bad(line, "unknown command"));                                         /* the reason is cut, never overruns */
    EXPECT(strlen(E) < sizeof E);
    line[ADMIN_MAX_LINE] = 'a';
    line[ADMIN_MAX_LINE + 1] = '\0';
    EXPECT(bad(line, "too long"));

    printf("parse: a long unknown word is cut in the reason; a tiny error buffer is never overrun\n");
    EXPECT(bad("thisisaverylongcommandwordthatgoesonandon", "unknown command 'thisisaverylongcomma'"));
    char small[8];
    memset(small, 'Z', sizeof small);
    EXPECT(admin_parse("vlan 1 4095", NP, &C, small, 5) == -1);
    EXPECT(strlen(small) <= 4 && small[5] == 'Z' && small[7] == 'Z');             /* only the first 5 bytes may be written */
    EXPECT(admin_parse(NULL, NP, &C, E, sizeof E) == -1);
}

static void test_no_side_effects(void)
{
    printf("parse: a refused command leaves no command behind that could be executed\n");
    /* admin_parse returns -1 for every refused line; the caller never looks at *cmd then. A good line after a bad one is parsed from scratch. */
    EXPECT(parse("vlan 1 0") == -1);
    EXPECT(parse("vlan 1 5") == 0 && C.kind == CMD_VLAN && C.arg == 5);
    EXPECT(parse("mirror 1 1") == -1);
    EXPECT(parse("mirror 1 2") == 0 && C.kind == CMD_MIRROR && C.arg == 2);
    EXPECT(parse("show mac") == 0 && C.port == 0 && C.arg == 0);                  /* unused fields are zero */
}

int main(void)
{
    test_valid();
    test_invalid();
    test_characters_and_length();
    test_no_side_effects();
    printf("\n%d checks, %d failures\n", checks, failures);
    printf(failures == 0 ? "ALL PASSED\n" : "SOME FAILED\n");
    return failures == 0 ? 0 : 1;
}
