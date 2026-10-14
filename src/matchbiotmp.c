/*
 * matchbiotmp - self-contained biometric template matcher (ZKTeco ZKFinger SDK)
 *
 * Takes two fingerprint templates and prints the matching score using
 * ZKFPM_DBMatch from the bundled ZKFinger SDK. The required shared
 * libraries are embedded in this ELF and extracted to a private temp
 * directory at startup (a re-exec is used so the dynamic linker can find
 * them via LD_LIBRARY_PATH).
 *
 * The algorithm engine (Innovatrics IDKit inside libzkfinger10.so) needs a
 * license. ZKTeco ships a ZKSOFTWARE license blob embedded inside
 * libzkfinger10.so; we apply it with IEngine_InitWithLicense() before the
 * engine is initialised.
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <dlfcn.h>
#include <unistd.h>
#include <errno.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <fcntl.h>
#include <ctype.h>

/* ------------------------------------------------------------------ */
/* embedded shared libraries (objcopy -I binary)                       */
/* ------------------------------------------------------------------ */
extern const unsigned char _binary_libzkfp_so_start[];
extern const unsigned char _binary_libzkfp_so_end[];
extern const unsigned char _binary_libzkfinger10_so_start[];
extern const unsigned char _binary_libzkfinger10_so_end[];
extern const unsigned char _binary_libsilkidcap_so_start[];
extern const unsigned char _binary_libsilkidcap_so_end[];
extern const unsigned char _binary_libidkit_so_2_start[];
extern const unsigned char _binary_libidkit_so_2_end[];
extern const unsigned char _binary_libsqlite3_so_0_start[];
extern const unsigned char _binary_libsqlite3_so_0_end[];
extern const unsigned char _binary_libcrypto_so_0_9_8_start[];
extern const unsigned char _binary_libcrypto_so_0_9_8_end[];
extern const unsigned char _binary_libiomp5_so_start[];
extern const unsigned char _binary_libiomp5_so_end[];
extern const unsigned char _binary_libusb_0_1_so_4_start[];
extern const unsigned char _binary_libusb_0_1_so_4_end[];
extern const unsigned char _binary_libusb_1_0_so_0_start[];
extern const unsigned char _binary_libusb_1_0_so_0_end[];

/* ZKSOFTWARE license blob (196 bytes) extracted from libzkfinger10.so */
static const unsigned char kLicense[] = {
    0x49, 0x43, 0x5f, 0x4c, 0x03, 0x00, 0x44, 0x00, 0x01, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x49, 0x30, 0x30, 0x30, 0x30, 0x30, 0x30, 0x30, 0x30, 0x30,
    0x30, 0x30, 0x30, 0x30, 0x31, 0x37, 0x00, 0x00, 0x00, 0x00, 0x5a, 0x4b,
    0x53, 0x4f, 0x46, 0x54, 0x57, 0x41, 0x52, 0x45, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x50, 0xc3, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x47, 0x7f, 0xf7, 0x06,
    0x1e, 0xd3, 0x15, 0x6b, 0x87, 0x49, 0xdc, 0xb9, 0xfa, 0xdb, 0x6c, 0x41,
    0x72, 0xff, 0x0c, 0xce, 0xbe, 0x73, 0xec, 0xa4, 0x23, 0xe2, 0xb5, 0x77,
    0x51, 0x70, 0xa6, 0x07, 0x21, 0xa4, 0xc2, 0x8e, 0x1d, 0xdd, 0xd2, 0x20,
    0xc5, 0xf2, 0x2d, 0x04, 0xcc, 0x0f, 0x01, 0x8e, 0x78, 0x40, 0xdc, 0x67,
    0xf9, 0x17, 0xde, 0xed, 0xc3, 0x90, 0x14, 0x4d, 0x18, 0x0e, 0x2c, 0xbd,
    0x83, 0x75, 0x76, 0x44, 0xe8, 0xfb, 0xd3, 0xfc, 0x52, 0xc3, 0x5e, 0x3c,
    0x79, 0xdf, 0x33, 0x4e, 0x14, 0x14, 0xe2, 0x47, 0x8d, 0x35, 0xc4, 0x23,
    0x1f, 0xe9, 0x51, 0xa6, 0xe6, 0x0f, 0x48, 0xbb, 0x90, 0xe1, 0x52, 0x55,
    0x4b, 0xfc, 0x33, 0x51, 0x48, 0xc7, 0x36, 0xe0, 0xe6, 0x74, 0xb1, 0x79,
    0x4e, 0x67, 0x6b, 0x75, 0xe5, 0x7e, 0x57, 0x14, 0x9a, 0x87, 0xdb, 0x63,
    0x4c, 0xb9, 0x8b, 0x8f,
};
/* ------------------------------------------------------------------ */
/* options                                                             */
/* ------------------------------------------------------------------ */
static int   opt_quiet = 0;
static int   opt_verbose = 0;
static int   opt_all = 0;
static int   opt_limit = 10;
static int   opt_threshold = 35;
static int   opt_width = 256;
static int   opt_height = 360;
static const char *opt_csv = NULL;
static const char *opt_format = "auto"; /* raw|b64|hex|auto */
static const char *opt_emps[8];
static int   opt_nemp = 0;
static int   opt_rows[8];
static int   opt_nrow = 0;

#define MAX_TPL 8192

struct tpl {
    unsigned char data[MAX_TPL];
    int len;
    char label[64];
};

/* ------------------------------------------------------------------ */
/* small helpers                                                       */
/* ------------------------------------------------------------------ */
static void die(const char *msg) { fprintf(stderr, "matchbiotmp: %s\n", msg); exit(1); }

static int b64val(int c)
{
    if (c >= 'A' && c <= 'Z') return c - 'A';
    if (c >= 'a' && c <= 'z') return c - 'a' + 26;
    if (c >= '0' && c <= '9') return c - '0' + 52;
    if (c == '+' || c == '-') return 62;
    if (c == '/' || c == '_') return 63;
    return -1;
}

static int b64decode(const char *s, unsigned char *out)
{
    int val = 0, bits = 0;
    unsigned char *p = out;
    for (; *s; s++) {
        if (*s == '=') break;
        if (isspace((unsigned char)*s)) continue;
        int c = b64val((unsigned char)*s);
        if (c < 0) return -1;
        val = (val << 6) | c;
        bits += 6;
        if (bits >= 8) {
            bits -= 8;
            *p++ = (val >> bits) & 0xff;
        }
    }
    return (int)(p - out);
}

static int hexval(int c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static int hexdecode(const char *s, unsigned char *out)
{
    if (s[0] == '0' && (s[1] == 'x' || s[1] == 'X')) s += 2;
    int hi = -1, n = 0;
    for (; *s; s++) {
        if (isspace((unsigned char)*s)) continue;
        int c = hexval((unsigned char)*s);
        if (c < 0) return -1;
        if (hi < 0) hi = c;
        else { out[n++] = (hi << 4) | c; hi = -1; }
    }
    if (hi >= 0) return -1;
    return n;
}

static int looks_text(const unsigned char *b, int n)
{
    if (n == 0) return 0;
    for (int i = 0; i < n; i++)
        if (!(isprint(b[i]) || isspace(b[i]))) return 0;
    return 1;
}

static int looks_base64(const char *s)
{
    int n = 0, pad = 0, nonws = 0;
    for (; *s; s++) {
        if (isspace((unsigned char)*s)) continue;
        nonws++;
        if (*s == '=') { pad++; continue; }
        if (pad) return 0;
        if (b64val((unsigned char)*s) < 0) return 0;
        n++;
    }
    if (nonws < 16) return 0;
    return (n % 4) != 1;
}

static int looks_hex(const char *s)
{
    if (s[0] == '0' && (s[1] == 'x' || s[1] == 'X')) s += 2;
    int n = 0;
    for (; *s; s++) {
        if (isspace((unsigned char)*s)) continue;
        if (hexval((unsigned char)*s) < 0) return 0;
        n++;
    }
    return n > 0 && (n % 2) == 0;
}

/* read a whole file, returns malloc'd buffer and length */
static unsigned char *read_file(const char *path, long *out_len)
{
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    fseek(f, 0, SEEK_END);
    long len = ftell(f);
    fseek(f, 0, SEEK_SET);
    if (len < 0) { fclose(f); return NULL; }
    unsigned char *buf = malloc((len ? len : 1) + 1);
    if (!buf) { fclose(f); return NULL; }
    if (len && fread(buf, 1, len, f) != (size_t)len) { free(buf); fclose(f); return NULL; }
    fclose(f);
    buf[len] = '\0';
    *out_len = len;
    return buf;
}

static void set_label(char *dst, const char *src)
{
    size_t n = strlen(src);
    if (n > 63) n = 63;
    memcpy(dst, src, n);
    dst[n] = '\0';
}

/* ------------------------------------------------------------------ */
/* template parsing                                                    */
/* ------------------------------------------------------------------ */
static int parse_template(const char *arg, struct tpl *t)
{
    const unsigned char *buf = NULL;
    long len = 0;
    unsigned char *heap = NULL;

    /* explicit prefixes */
    if (strncmp(arg, "b64:", 4) == 0) {
        int n = b64decode(arg + 4, t->data);
        if (n < 0) return -1;
        t->len = n; return 0;
    }
    if (strncmp(arg, "hex:", 4) == 0) {
        int n = hexdecode(arg + 4, t->data);
        if (n < 0) return -1;
        t->len = n; return 0;
    }
    if (strncmp(arg, "raw:", 4) == 0) {
        int n = strlen(arg + 4);
        if (n > MAX_TPL) return -1;
        memcpy(t->data, arg + 4, n); t->len = n; return 0;
    }

    /* existing file? */
    FILE *probe = fopen(arg, "rb");
    if (probe) {
        fclose(probe);
        heap = read_file(arg, &len);
        if (!heap) return -1;
        buf = heap;
    } else {
        /* literal string, not a file */
        if (strcmp(opt_format, "hex") == 0 || (strcmp(opt_format, "auto") == 0 && looks_hex(arg))) {
            int n = hexdecode(arg, t->data);
            if (n < 0) return -1;
            t->len = n; return 0;
        }
        int n = b64decode(arg, t->data);
        if (n <= 0) return -1;
        t->len = n; return 0;
    }

    /* decide how to interpret file content */
    const char *fmt = opt_format;
    if (strcmp(fmt, "raw") == 0) {
        if (len > MAX_TPL) return -1;
        memcpy(t->data, buf, len); t->len = (int)len; free(heap); return 0;
    }
    if (strcmp(fmt, "b64") == 0 || (strcmp(fmt, "auto") == 0 && looks_text(buf, len) && looks_base64((char *)buf))) {
        int n = b64decode((char *)buf, t->data);
        free(heap);
        if (n <= 0) return -1;
        t->len = n; return 0;
    }
    if (strcmp(fmt, "hex") == 0) {
        int n = hexdecode((char *)buf, t->data);
        free(heap);
        if (n < 0) return -1;
        t->len = n; return 0;
    }
    if (len > MAX_TPL) { free(heap); return -1; }
    memcpy(t->data, buf, len); t->len = (int)len; free(heap); return 0;
}

/* ------------------------------------------------------------------ */
/* CSV                                                                 */
/* ------------------------------------------------------------------ */
static int csv_split(char *line, char **out, int max)
{
    int n = 0; char *p = line;
    while (n < max) {
        while (*p == ' ' || *p == '\t') p++;
        char *start;
        if (*p == '"') {
            p++; start = p;
            while (*p && !(*p == '"' && (p[1] == ',' || p[1] == '\0'))) p++;
            if (*p == '"') { *p = '\0'; p++; }
            if (*p == ',') p++;
            out[n++] = start;
        } else {
            start = p;
            while (*p && *p != ',' && *p != '\n' && *p != '\r') p++;
            int end = (*p == '\0');
            *p = '\0';
            out[n++] = start;
            if (!end) p++;
        }
        if (p[-1] == '\0' && *p == '\0') break;
    }
    return n;
}

static int csv_load(const char *path, struct tpl *tpls, char labels[][64], int max)
{
    FILE *f = fopen(path, "rb");
    if (!f) { fprintf(stderr, "matchbiotmp: cannot open CSV '%s'\n", path); exit(1); }
    char line[65536];
    int n = 0, tmpl_col = 1, emp_col = 0, header = 1;
    while (fgets(line, sizeof line, f) && n < max) {
        char *fields[8];
        int nf = csv_split(line, fields, 8);
        if (nf < 2) continue;
        if (header) {
            for (int i = 0; i < nf; i++) {
                if (strcasecmp(fields[i], "actual_template") == 0) tmpl_col = i;
                if (strcasecmp(fields[i], "emp_code") == 0) emp_col = i;
            }
            header = 0;
            continue;
        }
        if (tmpl_col >= nf) continue;
        int len = b64decode(fields[tmpl_col], tpls[n].data);
        if (len <= 0) continue;
        tpls[n].len = len;
        set_label(labels[n], emp_col < nf ? fields[emp_col] : "?");
        n++;
    }
    fclose(f);
    return n;
}

/* ------------------------------------------------------------------ */
/* engine                                                              */
/* ------------------------------------------------------------------ */
typedef void *(*dbinit_t)(void);
typedef int (*dbmatch_t)(void *, unsigned char *, unsigned int, unsigned char *, unsigned int);
typedef int (*dbadd_t)(void *, unsigned int, unsigned char *, unsigned int);
typedef int (*dbident_t)(void *, unsigned char *, unsigned int, unsigned int *, unsigned int *);
typedef int (*initfp_t)(int, int);
typedef int (*initlic_t)(const char *, int);

static void *g_db = NULL;
static dbmatch_t g_match = NULL;
static dbadd_t g_add = NULL;
static dbident_t g_ident = NULL;

static void engine_init(const char *libdir)
{
    (void)libdir;

    void *hid = dlopen("libidkit.so.2", RTLD_NOW | RTLD_GLOBAL);
    if (!hid) { fprintf(stderr, "matchbiotmp: dlopen libidkit: %s\n", dlerror()); exit(1); }
    initlic_t initlic = (initlic_t)dlsym(hid, "IEngine_InitWithLicense");
    if (initlic) {
        int r = initlic((const char *)kLicense, sizeof(kLicense));
        if (r != 0 && opt_verbose)
            fprintf(stderr, "matchbiotmp: IEngine_InitWithLicense -> %d\n", r);
    } else if (opt_verbose) {
        fprintf(stderr, "matchbiotmp: no IEngine_InitWithLicense symbol\n");
    }

    void *h = dlopen("libzkfp.so", RTLD_NOW | RTLD_GLOBAL);
    if (!h) { fprintf(stderr, "matchbiotmp: dlopen libzkfp: %s\n", dlerror()); exit(1); }

    dbinit_t dbinit = (dbinit_t)dlsym(h, "ZKFPM_DBInit");
    g_match = (dbmatch_t)dlsym(h, "ZKFPM_DBMatch");
    g_add   = (dbadd_t)dlsym(h, "ZKFPM_DBAdd");
    g_ident = (dbident_t)dlsym(h, "ZKFPM_DBIdentify");
    initfp_t initfp = (initfp_t)dlsym(h, "_ZN11CZKFPHelper6InitFPEii");
    if (!dbinit || !g_match || !initfp)
        die("required symbols not found in libzkfp.so");

    /* the SDK prints diagnostics on stdout; hide them unless verbose */
    int saved_fd = -1, null_fd = -1;
    if (!opt_verbose) {
        fflush(stdout);
        saved_fd = dup(1);
        null_fd = open("/dev/null", O_WRONLY);
        if (saved_fd >= 0 && null_fd >= 0) dup2(null_fd, 1);
    }

    g_db = dbinit();
    if (!g_db) die("ZKFPM_DBInit failed");
    initfp(opt_width, opt_height);

    if (saved_fd >= 0 && null_fd >= 0) {
        fflush(stdout);
        dup2(saved_fd, 1);
        close(saved_fd);
        close(null_fd);
    }

    void *handle = *(void **)g_db;
    if (!handle && opt_verbose)
        fprintf(stderr, "matchbiotmp: warning: engine handle is NULL\n");

    /*
     * ZKFPM_DBMatch clamps its result to 0 whenever the similarity is below
     * the helper's internal threshold. Lower it so we always observe the raw
     * similarity score; the MATCH/NO MATCH decision is made by the caller.
     */
    int *thr = (int *)((char *)g_db + 0x0c);
    if (thr && *thr > 0) *thr = 1;
}

/* ------------------------------------------------------------------ */
/* extraction / re-exec                                                */
/* ------------------------------------------------------------------ */
struct emb { const char *name; const unsigned char *start, *end; };

static const struct emb g_embedded[] = {
    { "libusb-1.0.so.0",   _binary_libusb_1_0_so_0_start,   _binary_libusb_1_0_so_0_end },
    { "libusb-0.1.so.4",   _binary_libusb_0_1_so_4_start,   _binary_libusb_0_1_so_4_end },
    { "libcrypto.so.0.9.8",_binary_libcrypto_so_0_9_8_start,_binary_libcrypto_so_0_9_8_end },
    { "libsqlite3.so.0",   _binary_libsqlite3_so_0_start,   _binary_libsqlite3_so_0_end },
    { "libiomp5.so",       _binary_libiomp5_so_start,       _binary_libiomp5_so_end },
    { "libidkit.so.2",     _binary_libidkit_so_2_start,     _binary_libidkit_so_2_end },
    { "libsilkidcap.so",   _binary_libsilkidcap_so_start,   _binary_libsilkidcap_so_end },
    { "libzkfinger10.so",  _binary_libzkfinger10_so_start,  _binary_libzkfinger10_so_end },
    { "libzkfp.so",        _binary_libzkfp_so_start,        _binary_libzkfp_so_end },
};

static int write_file(const char *path, const unsigned char *data, size_t len)
{
    FILE *f = fopen(path, "wb");
    if (!f) return -1;
    if (len && fwrite(data, 1, len, f) != len) { fclose(f); return -1; }
    fclose(f);
    chmod(path, 0644);
    return 0;
}

static const char *g_clean_dir = NULL;

static void cleanup_extract(void)
{
    if (!g_clean_dir) return;
    for (size_t i = 0; i < sizeof(g_embedded) / sizeof(g_embedded[0]); i++) {
        char path[4600];
        snprintf(path, sizeof path, "%s/%s", g_clean_dir, g_embedded[i].name);
        unlink(path);
    }
    rmdir(g_clean_dir);
}

static void extract_and_reexec(char **argv)
{
    const char *base = getenv("TMPDIR");
    if (!base || !*base) base = "/tmp";
    char tmpl[4096];
    snprintf(tmpl, sizeof tmpl, "%s/matchbiotmp.XXXXXX", base);
    char *dir = mkdtemp(tmpl);
    if (!dir) { perror("matchbiotmp: mkdtemp"); exit(1); }

    for (size_t i = 0; i < sizeof(g_embedded) / sizeof(g_embedded[0]); i++) {
        char path[4600];
        snprintf(path, sizeof path, "%s/%s", dir, g_embedded[i].name);
        if (write_file(path, g_embedded[i].start, g_embedded[i].end - g_embedded[i].start) != 0) {
            fprintf(stderr, "matchbiotmp: cannot write %s: %s\n", path, strerror(errno));
            exit(1);
        }
    }

    const char *cur = getenv("LD_LIBRARY_PATH");
    char lp[8192];
    if (cur && *cur) snprintf(lp, sizeof lp, "%s:%s", dir, cur);
    else snprintf(lp, sizeof lp, "%s", dir);
    setenv("LD_LIBRARY_PATH", lp, 1);
    setenv("MATCHBIOTMP_LIBS", dir, 1);
    setenv("MATCHBIOTMP_CLEAN", "1", 1);

    char *exe = "/proc/self/exe";
    execv(exe, argv);
    execvp(argv[0], argv);
    perror("matchbiotmp: re-exec");
    exit(1);
}

/* ------------------------------------------------------------------ */
/* usage                                                               */
/* ------------------------------------------------------------------ */
static void usage(FILE *f)
{
    fprintf(f,
"Usage:\n"
"  matchbiotmp [options] <T1> <T2>              match two templates\n"
"  matchbiotmp [options] --csv F --emp A --emp B  match two employees\n"
"  matchbiotmp [options] --csv F --all            pairwise score matrix\n"
"\n"
"Templates may be: a file (raw / base64 / hex), a literal base64 string,\n"
"or prefixed with b64: / hex: / raw: .\n"
"\n"
"When --emp (or --row) is given twice, every template enrolled for the first\n"
"employee is compared against every template of the second; the reported\n"
"score is the best (highest) of those comparisons.\n"
"\n"
"Options:\n"
"  -i, --input-format raw|b64|hex|auto  interpretation of file input (auto)\n"
"  -t, --threshold N     match threshold, 0..100 (default 35)\n"
"      --width N         algorithm image width (default 256)\n"
"      --height N        algorithm image height (default 360)\n"
"      --csv FILE        template CSV (emp_code,actual_template,...)\n"
"      --emp CODE        employee code (repeat twice; all templates compared)\n"
"      --row N           1-based data row (repeat twice)\n"
"      --all             score every pair among CSV rows\n"
"      --limit N         max rows for --all (default 10)\n"
"  -q, --quiet           print only the score\n"
"  -v, --verbose         extra diagnostics on stderr\n"
"  -h, --help            this help\n");
}

/* ------------------------------------------------------------------ */
int main(int argc, char **argv)
{
    const char *libdir = getenv("MATCHBIOTMP_LIBS");

    int i = 1;
    for (; i < argc; i++) {
        const char *a = argv[i];
        if (a[0] != '-' || a[1] == '\0') break;
        if (!strcmp(a, "--")) { i++; break; }
        else if (!strcmp(a, "-q") || !strcmp(a, "--quiet")) opt_quiet = 1;
        else if (!strcmp(a, "-v") || !strcmp(a, "--verbose")) opt_verbose = 1;
        else if (!strcmp(a, "-h") || !strcmp(a, "--help")) { usage(stdout); return 0; }
        else if (!strcmp(a, "--all")) opt_all = 1;
        else if (!strcmp(a, "-t") || !strcmp(a, "--threshold")) { opt_threshold = atoi(argv[++i]); }
        else if (!strcmp(a, "--width")) opt_width = atoi(argv[++i]);
        else if (!strcmp(a, "--height")) opt_height = atoi(argv[++i]);
        else if (!strcmp(a, "--limit")) opt_limit = atoi(argv[++i]);
        else if (!strcmp(a, "--csv")) opt_csv = argv[++i];
        else if (!strcmp(a, "--emp")) { if (opt_nemp < 8) opt_emps[opt_nemp++] = argv[++i]; }
        else if (!strcmp(a, "--row")) { if (opt_nrow < 8) opt_rows[opt_nrow++] = atoi(argv[++i]); }
        else if (!strcmp(a, "-i") || !strcmp(a, "--input-format")) opt_format = argv[++i];
        else { fprintf(stderr, "matchbiotmp: unknown option '%s'\n", a); usage(stderr); return 1; }
    }

    if (opt_threshold < 0 || opt_threshold > 100) die("threshold must be 0..100");

    if (!libdir)
        extract_and_reexec(argv);

    if (getenv("MATCHBIOTMP_CLEAN")) {
        unsetenv("MATCHBIOTMP_CLEAN");
        g_clean_dir = libdir;
        atexit(cleanup_extract);
    }

    /* ----- gather templates ----- */
    static struct tpl tpls[512];
    static char labels[512][64];
    int nt = 0;

    /* two groups of templates compared as a cross product (CSV mode) */
    static struct tpl allt[8192];
    static char alll[8192][64];
    static int grp1[4096], grp2[4096];
    int ngrp1 = 0, ngrp2 = 0, use_groups = 0;
    char g1name[64] = "set1", g2name[64] = "set2";

    if (opt_csv && opt_all) {
        nt = csv_load(opt_csv, tpls, labels, opt_limit < 512 ? opt_limit : 512);
    } else if (opt_csv) {
        int total = csv_load(opt_csv, allt, alll, 8192);
        if (opt_nemp == 2) {
            for (int j = 0; j < total; j++) {
                if (ngrp1 < 4096 && !strcmp(alll[j], opt_emps[0])) grp1[ngrp1++] = j;
                if (ngrp2 < 4096 && !strcmp(alll[j], opt_emps[1])) grp2[ngrp2++] = j;
            }
            if (!ngrp1) { fprintf(stderr, "matchbiotmp: employee '%s' not found in CSV\n", opt_emps[0]); return 1; }
            if (!ngrp2) { fprintf(stderr, "matchbiotmp: employee '%s' not found in CSV\n", opt_emps[1]); return 1; }
            set_label(g1name, opt_emps[0]);
            set_label(g2name, opt_emps[1]);
        } else if (opt_nrow == 2) {
            int r1 = opt_rows[0] - 1, r2 = opt_rows[1] - 1;
            if (r1 < 0 || r1 >= total) { fprintf(stderr, "matchbiotmp: row %d out of range (1..%d)\n", opt_rows[0], total); return 1; }
            if (r2 < 0 || r2 >= total) { fprintf(stderr, "matchbiotmp: row %d out of range (1..%d)\n", opt_rows[1], total); return 1; }
            grp1[ngrp1++] = r1;
            grp2[ngrp2++] = r2;
            snprintf(g1name, sizeof g1name, "row %d", opt_rows[0]);
            snprintf(g2name, sizeof g2name, "row %d", opt_rows[1]);
        } else {
            fprintf(stderr, "matchbiotmp: CSV mode needs two --emp, two --row, or --all\n");
            return 1;
        }
        use_groups = 1;
    } else {
        int nargs = argc - i;
        if (nargs != 2) { usage(stderr); return 1; }
        for (int k = 0; k < 2; k++) {
            const char *arg = argv[i + k];
            if (access(arg, R_OK) == 0) {
                set_label(labels[nt], arg);
            } else {
                char tmp[64];
                snprintf(tmp, sizeof tmp, "template%d", k + 1);
                set_label(labels[nt], tmp);
            }
            if (parse_template(arg, &tpls[nt]) != 0) {
                fprintf(stderr, "matchbiotmp: cannot parse template '%s'\n", arg);
                return 1;
            }
            nt++;
        }
    }

    engine_init(libdir);

    if (opt_all) {
        if (!opt_quiet)
            printf("# pairwise match scores (threshold %d), %d templates\n", opt_threshold, nt);
        printf("emp_code");
        for (int c = 0; c < nt; c++) printf(",%s", labels[c]);
        printf("\n");
        for (int r = 0; r < nt; r++) {
            printf("%s", labels[r]);
            for (int c = 0; c < nt; c++) {
                int s = g_match(g_db, tpls[r].data, tpls[r].len, tpls[c].data, tpls[c].len);
                printf(",%d", s);
            }
            printf("\n");
        }
        return 0;
    }

    if (use_groups) {
        if (!opt_quiet) {
            printf("%s: %d template(s)\n", g1name, ngrp1);
            printf("%s: %d template(s)\n", g2name, ngrp2);
            fflush(stdout);
        }
        int best = -1, pairs = 0;
        for (int a = 0; a < ngrp1; a++) {
            for (int b = 0; b < ngrp2; b++) {
                if (grp1[a] == grp2[b]) continue; /* never compare a template with itself */
                int s = g_match(g_db, allt[grp1[a]].data, allt[grp1[a]].len,
                                allt[grp2[b]].data, allt[grp2[b]].len);
                pairs++;
                if (s > best) best = s;
                if (opt_verbose)
                    fprintf(stderr, "  %s template #%d vs %s template #%d: %d\n",
                            g1name, grp1[a] + 1, g2name, grp2[b] + 1, s);
            }
        }
        if (pairs == 0) {
            fprintf(stderr, "matchbiotmp: no distinct template pairs to compare\n");
            if (opt_quiet) printf("0\n");
            else printf("Score: 0\nResult: NO MATCH\n");
            return 0;
        }
        if (opt_quiet) {
            printf("%d\n", best);
        } else {
            printf("Best score: %d\n", best);
            printf("Pairs compared: %d\n", pairs);
            printf("Threshold: %d\n", opt_threshold);
            printf("Result: %s\n", best >= opt_threshold ? "MATCH" : "NO MATCH");
        }
        return 0;
    }

    int score = g_match(g_db, tpls[0].data, tpls[0].len, tpls[1].data, tpls[1].len);
    if (opt_quiet) {
        printf("%d\n", score);
    } else {
        printf("Template1: %s (%d bytes)\n", labels[0], tpls[0].len);
        printf("Template2: %s (%d bytes)\n", labels[1], tpls[1].len);
        printf("Score: %d\n", score);
        printf("Threshold: %d\n", opt_threshold);
        printf("Result: %s\n", score >= opt_threshold ? "MATCH" : "NO MATCH");
    }
    return 0;
}
