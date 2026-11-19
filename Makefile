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

.PHONY: all tsan clean test test-valgrind test-tsan
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

# make test = ./tests/run_all.sh (normal variant: Parts 1-7 suite + the ten integration scripts)
test:
	./tests/run_all.sh

test-valgrind:
	VARIANT=valgrind ./tests/run_all.sh

test-tsan:
	VARIANT=tsan ./tests/run_all.sh

clean:
	rm -rf build $(PROGRAMS) $(TSAN_PROGRAMS) switch_small switch_small_tsan

-include $(wildcard $(OBJ_DIR)/*.d $(TSAN_DIR)/*.d $(SMALL_DIR)/*.d $(SMALL_TSAN_DIR)/*.d build/*.d)
