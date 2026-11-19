# Virtual Switch Makefile
CC      = gcc
CFLAGS  = -std=c11 -Wall -Wextra -Werror -g -O0 -D_GNU_SOURCE -pthread -Iinclude
LDLIBS  = -pthread -lrt

# Shared code used by every program (grows in later parts).
COMMON_SRC = src/util.c src/log.c src/frame.c src/queue.c src/stats.c

PROGRAMS      = switch host swctl supervisor
TSAN_PROGRAMS = switch_tsan host_tsan

# Objects that only the switch (and the tests of its internals) need.
SWITCH_NAMES = port forward mactable sig config reload admin

OBJ_DIR   = build/obj
TSAN_DIR  = build/tsan
SMALL_DIR = build/small
SMALL_TSAN_DIR = build/small_tsan

COMMON_OBJ = $(patsubst src/%.c,$(OBJ_DIR)/%.o,$(COMMON_SRC))
TSAN_OBJ   = $(patsubst src/%.c,$(TSAN_DIR)/%.o,$(COMMON_SRC))

.PHONY: all tsan clean test test-valgrind test-tsan bench bench-clean
all: $(PROGRAMS)
tsan: $(TSAN_PROGRAMS) switch_small_tsan

# Extra objects that only the switch needs.
switch: $(patsubst %,$(OBJ_DIR)/%.o,$(SWITCH_NAMES))
switch_tsan: $(patsubst %,$(TSAN_DIR)/%.o,$(SWITCH_NAMES))

# ---- normal build ----
$(PROGRAMS): %: $(OBJ_DIR)/%.o $(COMMON_OBJ)
	$(CC) $(CFLAGS) -o $@ $^ $(LDLIBS)

$(OBJ_DIR)/%.o: src/%.c
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS) -MMD -MP -c -o $@ $<

# ---- ThreadSanitizer build (objects kept apart in build/tsan) ----
$(TSAN_PROGRAMS): %_tsan: $(TSAN_DIR)/%.o $(TSAN_OBJ)
	$(CC) $(CFLAGS) -fsanitize=thread -o $@ $^ $(LDLIBS)

$(TSAN_DIR)/%.o: src/%.c
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS) -fsanitize=thread -MMD -MP -c -o $@ $<

# ---- switch_small: a switch for the tests with a tiny MAC table (K=4, 4 buckets),
# aging after 3 s and at most 3 MACs per port. It is the same source; only the
# defaults differ. (Until the config file exists in Part 10 these cannot be set at run time.)
SMALL_FLAGS = -DDEFAULT_MAC_TABLE_SIZE=4 -DDEFAULT_MAC_BUCKETS=4 -DDEFAULT_MAC_AGING_SEC=3 \
              -DDEFAULT_MAX_MACS_PER_PORT=3
SMALL_OBJ = $(patsubst src/%.c,$(SMALL_DIR)/%.o,$(COMMON_SRC)) \
            $(patsubst %,$(SMALL_DIR)/%.o,switch $(SWITCH_NAMES))

switch_small: $(SMALL_OBJ)
	$(CC) $(CFLAGS) $(SMALL_FLAGS) -o $@ $^ $(LDLIBS)

$(SMALL_DIR)/%.o: src/%.c
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS) $(SMALL_FLAGS) -MMD -MP -c -o $@ $<

# ---- switch_small under ThreadSanitizer (Part 8: the aging / port security scripts need the small settings) ----
SMALL_TSAN_OBJ = $(patsubst src/%.c,$(SMALL_TSAN_DIR)/%.o,$(COMMON_SRC)) \
                 $(patsubst %,$(SMALL_TSAN_DIR)/%.o,switch $(SWITCH_NAMES))

switch_small_tsan: $(SMALL_TSAN_OBJ)
	$(CC) $(CFLAGS) $(SMALL_FLAGS) -fsanitize=thread -o $@ $^ $(LDLIBS)

$(SMALL_TSAN_DIR)/%.o: src/%.c
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS) $(SMALL_FLAGS) -fsanitize=thread -MMD -MP -c -o $@ $<

# ---- unit tests (tests/test_NAME.c -> build/test_NAME) ----
# tests of the switch internals need the switch-only objects too
build/test_port build/test_mactable build/test_vlan build/test_admin build/test_mirror build/test_macshm: $(patsubst %,$(OBJ_DIR)/%.o,$(SWITCH_NAMES))
build/test_port_tsan build/test_mactable_tsan build/test_vlan_tsan build/test_admin_tsan build/test_mirror_tsan build/test_macshm_tsan: $(patsubst %,$(TSAN_DIR)/%.o,$(SWITCH_NAMES))

# test_config tests the pure parser: it needs only config.o
build/test_config: $(OBJ_DIR)/config.o
build/test_config_tsan: $(TSAN_DIR)/config.o

build/test_%_tsan: tests/test_%.c $(TSAN_OBJ)
	@mkdir -p build
	$(CC) $(CFLAGS) -fsanitize=thread -o $@ $^ $(LDLIBS)

build/test_%: tests/test_%.c $(COMMON_OBJ)
	@mkdir -p build
	$(CC) $(CFLAGS) -MMD -MP -o $@ $^ $(LDLIBS)

# ---- Part 21: the benchmark (global lock vs striped locks vs rwlock in the MAC table) ----
# Everything is built with the same flags as the rest of the project (-O0 included). The three switches differ ONLY in mactable.c (-DMAC_LOCK=GLOBAL | STRIPED | RWLOCK);
# bench/bench is the network load generator, bench/bench_mac_<variant> the in-process micro-benchmark that links the variant of mactable.c directly.
BENCH_VARIANTS = global striped rwlock
BENCH_DIR      = build/bench
BENCH_TSAN_DIR = build/bench_tsan
BENCH_SWITCHES = $(patsubst %,switch_%,$(BENCH_VARIANTS))
BENCH_MAC      = $(patsubst %,bench/bench_mac_%,$(BENCH_VARIANTS))
uc = $(shell echo $(1) | tr a-z A-Z)
SWITCH_OTHER_OBJ = $(patsubst %,$(OBJ_DIR)/%.o,switch $(filter-out mactable,$(SWITCH_NAMES)))
MAC_OTHER_OBJ    = $(patsubst %,$(OBJ_DIR)/%.o,$(filter-out mactable,$(SWITCH_NAMES)))
MAC_OTHER_TSAN   = $(patsubst %,$(TSAN_DIR)/%.o,$(filter-out mactable,$(SWITCH_NAMES)))

bench: $(BENCH_SWITCHES) bench/bench $(BENCH_MAC)

$(BENCH_DIR)/mactable_%.o: src/mactable.c
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS) -DMAC_LOCK=$(call uc,$*) -MMD -MP -c -o $@ $<

$(BENCH_TSAN_DIR)/mactable_%.o: src/mactable.c
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS) -fsanitize=thread -DMAC_LOCK=$(call uc,$*) -MMD -MP -c -o $@ $<

$(BENCH_SWITCHES): switch_%: $(BENCH_DIR)/mactable_%.o $(SWITCH_OTHER_OBJ) $(COMMON_OBJ)
	$(CC) $(CFLAGS) -o $@ $^ $(LDLIBS)

bench/bench: bench/bench.c $(COMMON_OBJ)
	$(CC) $(CFLAGS) -MMD -MP -o $@ $^ $(LDLIBS)

$(BENCH_MAC): bench/bench_mac_%: bench/bench_mac.c $(BENCH_DIR)/mactable_%.o $(MAC_OTHER_OBJ) $(COMMON_OBJ)       # a STATIC pattern: a plain one would also match bench_mac_<v>.d
	$(CC) $(CFLAGS) -DBENCH_VARIANT='"$*"' -MMD -MP -o $@ $^ $(LDLIBS)

# the unit tests of the MAC table against the other two locking strategies (the default build is tested by build/test_mactable)
build/test_mactable_global build/test_mactable_rwlock: build/test_mactable_%: tests/test_mactable.c $(BENCH_DIR)/mactable_%.o $(MAC_OTHER_OBJ) $(COMMON_OBJ)
	@mkdir -p build
	$(CC) $(CFLAGS) -o $@ $^ $(LDLIBS)

build/test_mactable_global_tsan build/test_mactable_rwlock_tsan: build/test_mactable_%_tsan: tests/test_mactable.c $(BENCH_TSAN_DIR)/mactable_%.o $(MAC_OTHER_TSAN) $(TSAN_OBJ)
	@mkdir -p build
	$(CC) $(CFLAGS) -fsanitize=thread -o $@ $^ $(LDLIBS)

bench-clean:
	rm -f $(BENCH_SWITCHES) bench/bench $(BENCH_MAC)

# make test = ./tests/run_all.sh (normal variant: Parts 1-7 suite + the ten integration scripts)
test:
	./tests/run_all.sh

test-valgrind:
	VARIANT=valgrind ./tests/run_all.sh

test-tsan:
	VARIANT=tsan ./tests/run_all.sh

clean:
	rm -rf build $(PROGRAMS) $(TSAN_PROGRAMS) switch_small switch_small_tsan $(BENCH_SWITCHES) bench/bench $(BENCH_MAC) bench/*.d

# Dependency files are never remade (without this, make tries to "link" an included X.d from X.d.o, which the bench pattern rules would happily build with a bogus -DMAC_LOCK).
%.d: ;

-include $(wildcard $(OBJ_DIR)/*.d $(TSAN_DIR)/*.d $(SMALL_DIR)/*.d $(SMALL_TSAN_DIR)/*.d build/*.d $(BENCH_DIR)/*.d $(BENCH_TSAN_DIR)/*.d bench/*.d)
