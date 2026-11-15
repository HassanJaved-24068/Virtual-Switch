# Virtual Switch Makefile
CC      = gcc
CFLAGS  = -std=c11 -Wall -Wextra -Werror -g -O0 -D_GNU_SOURCE -pthread -Iinclude
LDLIBS  = -pthread -lrt

# Shared code used by every program (grows in later parts).
COMMON_SRC = src/util.c src/log.c src/frame.c src/queue.c src/stats.c

PROGRAMS      = switch host swctl
TSAN_PROGRAMS = switch_tsan host_tsan

OBJ_DIR  = build/obj
TSAN_DIR = build/tsan

COMMON_OBJ = $(patsubst src/%.c,$(OBJ_DIR)/%.o,$(COMMON_SRC))
TSAN_OBJ   = $(patsubst src/%.c,$(TSAN_DIR)/%.o,$(COMMON_SRC))

.PHONY: all tsan clean test
all: $(PROGRAMS)
tsan: $(TSAN_PROGRAMS)

# Extra objects that only the switch needs.
switch: $(OBJ_DIR)/port.o $(OBJ_DIR)/forward.o
switch_tsan: $(TSAN_DIR)/port.o $(TSAN_DIR)/forward.o

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

# ---- unit tests (tests/test_NAME.c -> build/test_NAME) ----
# test_port needs the switch-only objects too
build/test_port: $(OBJ_DIR)/port.o $(OBJ_DIR)/forward.o
build/test_port_tsan: $(TSAN_DIR)/port.o $(TSAN_DIR)/forward.o

build/test_%_tsan: tests/test_%.c $(TSAN_OBJ)
	@mkdir -p build
	$(CC) $(CFLAGS) -fsanitize=thread -o $@ $^ $(LDLIBS)

build/test_%: tests/test_%.c $(COMMON_OBJ)
	@mkdir -p build
	$(CC) $(CFLAGS) -MMD -MP -o $@ $^ $(LDLIBS)

test:
	./tests/run_all.sh

clean:
	rm -rf build $(PROGRAMS) $(TSAN_PROGRAMS)

-include $(wildcard $(OBJ_DIR)/*.d $(TSAN_DIR)/*.d build/*.d)
