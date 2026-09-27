PREFIX ?= /usr/local
BINDIR ?= $(PREFIX)/bin
DESTDIR ?=
CC ?= cc
CFLAGS ?= -std=c11 -Wall -Wextra -Werror -O2
CJSON_CFLAGS = $(shell pkg-config --cflags libcjson sqlite3 zlib)
CJSON_LIBS = $(shell pkg-config --libs libcjson sqlite3 zlib)
CORE = src/util.c src/tools.c
HEADERS = src/common.h src/tools.h src/domain.h

.PHONY: all clean install test
all: arxiv-mcp arxiv
arxiv-mcp: src/main.c src/mcp_tools.c src/util.c $(HEADERS)
	$(CC) $(CFLAGS) $(CJSON_CFLAGS) -o $@ src/main.c src/mcp_tools.c src/util.c $(shell pkg-config --libs libcjson)
arxiv: src/cli.c $(CORE) $(HEADERS)
	$(CC) $(CFLAGS) $(CJSON_CFLAGS) -o $@ src/cli.c $(CORE) $(CJSON_LIBS)
backend_test: tests/backend_test.c $(CORE) $(HEADERS)
	$(CC) $(CFLAGS) $(CJSON_CFLAGS) -Isrc -o $@ tests/backend_test.c $(CORE) $(CJSON_LIBS)
protocol_test: tests/protocol_test.c src/util.c src/common.h
	$(CC) $(CFLAGS) $(CJSON_CFLAGS) -Isrc -o $@ tests/protocol_test.c src/util.c $(shell pkg-config --libs libcjson)
test: all backend_test protocol_test
	./backend_test
	./protocol_test
install: all
	install -d "$(DESTDIR)$(BINDIR)"
	install -m 755 arxiv-mcp arxiv "$(DESTDIR)$(BINDIR)/"
clean:
	rm -f arxiv-mcp arxiv backend_test protocol_test
