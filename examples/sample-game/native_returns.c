/* Owned x86-64 tail-call and recursive return-tracking fixture. */
#include <setjmp.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdlib.h>
static jmp_buf escape;
static _Atomic int ready;
static int exit_early;
__attribute__((noinline)) void checkpoint(void) { __asm__ volatile ("" ::: "memory"); }
__attribute__((noinline)) unsigned callee(unsigned value) { checkpoint(); if (exit_early) _Exit(0); return value+7; }
__attribute__((noinline)) unsigned tail(unsigned value) { return callee(value); }
__attribute__((noinline)) unsigned recurse(unsigned value) {
    if (!value) { checkpoint(); return 1; }
    unsigned result = recurse(value-1);
    __asm__ volatile ("" : "+r"(result));
    return result+1;
}
__attribute__((noinline)) unsigned escape_frame(unsigned value) {
    if (value) longjmp(escape, 1);
    return 0;
}
static void *worker(void *unused) {
    (void)unused;
    while (!atomic_load(&ready)) {}
    tail(3);
    return 0;
}
int main(int argc, char **argv) {
    (void)argv;
    if (argc > 2) { exit_early = 1; return tail(1); }
    if (argc > 1) {
        if (!setjmp(escape)) escape_frame(1);
        checkpoint();
        return 0;
    }
    pthread_t thread;
    pthread_create(&thread, 0, worker, 0);
    unsigned result = tail(1) + recurse(3);
    atomic_store(&ready, 1);
    pthread_join(thread, 0);
    return result == 12 ? 0 : 1;
}
