/* A toy set-associative LRU cache simulator: the example project for cloudsweep.
   usage: cachesim --sets N --ways W --accesses A --seed S [--trace FILE] --out DIR */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static unsigned long long state;
static unsigned long long next(void) { state ^= state << 13; state ^= state >> 7; state ^= state << 17; return state; }

int main(int argc, char **argv) {
    long sets = 64, ways = 4, accesses = 1000000; unsigned long long seed = 1;
    const char *out = NULL, *trace = NULL;
    for (int i = 1; i + 1 < argc; i += 2) {
        if (!strcmp(argv[i], "--sets")) sets = atol(argv[i + 1]);
        else if (!strcmp(argv[i], "--ways")) ways = atol(argv[i + 1]);
        else if (!strcmp(argv[i], "--accesses")) accesses = atol(argv[i + 1]);
        else if (!strcmp(argv[i], "--seed")) seed = strtoull(argv[i + 1], NULL, 10);
        else if (!strcmp(argv[i], "--trace")) trace = argv[i + 1];
        else if (!strcmp(argv[i], "--out")) out = argv[i + 1];
        else { fprintf(stderr, "unknown option %s\n", argv[i]); return 2; }
    }
    if (!out || sets < 1 || ways < 1) { fprintf(stderr, "usage: see sim.c\n"); return 2; }
    state = seed * 2654435761ULL + 1;
    long footprint = 4 * sets * ways;
    if (trace) {  /* the trace's first line sets the address footprint */
        FILE *t = fopen(trace, "r");
        if (!t || fscanf(t, "%ld", &footprint) != 1) { fprintf(stderr, "bad trace %s\n", trace); return 2; }
        fclose(t);
    }
    unsigned long long *tags = calloc(sets * ways, sizeof *tags), *age = calloc(sets * ways, sizeof *age);
    long hits = 0;
    for (long n = 1; n <= accesses; n++) {
        unsigned long long line = next() % footprint + 1, set = line % sets, *t = tags + set * ways, *a = age + set * ways;
        long victim = 0, hit = -1;
        for (long w = 0; w < ways; w++) { if (t[w] == line) hit = w; if (a[w] < a[victim]) victim = w; }
        if (hit >= 0) { hits++; a[hit] = n; } else { t[victim] = line; a[victim] = n; }
    }
    char path[4096];
    snprintf(path, sizeof path, "%s/result.json", out);
    FILE *f = fopen(path, "w");
    if (!f) { perror(path); return 1; }
    fprintf(f, "{\"sets\": %ld, \"ways\": %ld, \"accesses\": %ld, \"seed\": %llu, \"footprint\": %ld, "
               "\"hits\": %ld, \"hit_rate\": %.6f}\n", sets, ways, accesses, seed, footprint, hits, (double)hits / accesses);
    fclose(f);
    printf("hit rate %.4f\n", (double)hits / accesses);
    return 0;
}
