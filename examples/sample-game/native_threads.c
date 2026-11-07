/* Owned native debugger fixture: concurrent workers, watched stores and a return. */
#define _GNU_SOURCE
#include <pthread.h>
#include <stdint.h>
#include <stdatomic.h>
#include <unistd.h>
volatile uint32_t watched;
_Atomic int done;
_Atomic unsigned long pulses[2];
__attribute__((noinline)) unsigned step(unsigned value) {
    watched = value;
    return value + 7;
}
void *spinner(void *arg) {
    unsigned long i = (unsigned long)arg;
    while (!atomic_load(&done)) atomic_fetch_add(&pulses[i], 1);
    return 0;
}
void *writer(void *unused) {
    usleep(20000);
    for (unsigned i = 1; i <= 3; ++i) if (step(i) != i + 7) _exit(2);
    return 0;
}
int main(void) {
    pthread_t s[2], w;
    pthread_create(&s[0], 0, spinner, (void *)0);
    pthread_create(&s[1], 0, spinner, (void *)1);
    pthread_create(&w, 0, writer, 0);
    pthread_join(w, 0);
    atomic_store(&done, 1);
    pthread_join(s[0], 0); pthread_join(s[1], 0);
    return watched == 3 ? 0 : 1;
}
