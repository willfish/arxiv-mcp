#include "common.h"

#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdarg.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

Result result_ok(char *text) {
  Result r = {text ? text : strdup(""), 0};
  return r;
}

Result result_err(const char *msg) {
  Result r = {strdup(msg ? msg : "error"), 1};
  return r;
}

Result result_errf(const char *fmt, ...) {
  char buf[1024];
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(buf, sizeof buf, fmt, ap);
  va_end(ap);
  return result_err(buf);
}

void result_free(Result r) { free(r.text); }

void argv_init(Argv *a) {
  a->v = NULL;
  a->n = 0;
  a->cap = 0;
}

void argv_add(Argv *a, const char *s) {
  if (a->n + 1 >= a->cap) {
    a->cap = a->cap ? a->cap * 2 : 8;
    a->v = realloc(a->v, a->cap * sizeof *a->v);
  }
  a->v[a->n++] = s ? strdup(s) : NULL;
}

void argv_free(Argv *a) {
  for (size_t i = 0; i < a->n; i++) free(a->v[i]);
  free(a->v);
  a->v = NULL;
  a->n = a->cap = 0;
}

void capture_free(Capture *c) {
  free(c->out);
  free(c->err);
  c->out = c->err = NULL;
}

static int timeout_sec(void) {
  const char *t = getenv("ARXIV_TIMEOUT");
  int n = t && *t ? atoi(t) : 60;
  return n > 0 ? n : 60;
}

static int append_fd(char **buf, size_t *len, size_t *cap, int fd, size_t max) {
  char tmp[4096];
  ssize_t n = read(fd, tmp, sizeof tmp);
  if (n < 0) return errno == EAGAIN || errno == EWOULDBLOCK ? 0 : -1;
  if (n == 0) return 1;
  if (*len + (size_t)n > max) return -2;
  if (*len + (size_t)n + 1 > *cap) {
    *cap = (*cap ? *cap : 4096);
    while (*len + (size_t)n + 1 > *cap) *cap *= 2;
    *buf = realloc(*buf, *cap);
  }
  memcpy(*buf + *len, tmp, (size_t)n);
  *len += (size_t)n;
  (*buf)[*len] = 0;
  return 0;
}

int run_cmd(char *const argv[], Capture *cap) {
  return run_cmd_input(argv, cap, NULL);
}

int run_cmd_input(char *const argv[], Capture *cap, const char *input) {
  /* Anonymous file avoids pipe deadlocks for large templates and argv exposure. */
  FILE *in = tmpfile();
  if (!in) return -1;
  if (input && fwrite(input, 1, strlen(input), in) != strlen(input)) {
    fclose(in);
    return -1;
  }
  if (fflush(in) || fseek(in, 0, SEEK_SET)) { fclose(in); return -1; }
  int outp[2], errp[2];
  if (pipe(outp) < 0) { fclose(in); return -1; }
  if (pipe(errp) < 0) { close(outp[0]); close(outp[1]); fclose(in); return -1; }
  pid_t pid = fork();
  if (pid < 0) {
    close(outp[0]); close(outp[1]); close(errp[0]); close(errp[1]); fclose(in);
    return -1;
  }
  if (pid == 0) {
    setpgid(0, 0);
    dup2(outp[1], STDOUT_FILENO);
    dup2(errp[1], STDERR_FILENO);
    if (dup2(fileno(in), STDIN_FILENO) < 0) _exit(126);
    fclose(in);
    close(outp[0]);
    close(outp[1]);
    close(errp[0]);
    close(errp[1]);
    execvp(argv[0], argv);
    dprintf(STDERR_FILENO, "exec %s: %s\n", argv[0], strerror(errno));
    _exit(127);
  }
  fclose(in);
  setpgid(pid, pid);
  close(outp[1]);
  close(errp[1]);
  fcntl(outp[0], F_SETFL, O_NONBLOCK);
  fcntl(errp[0], F_SETFL, O_NONBLOCK);
  cap->out = calloc(1, 1);
  cap->err = calloc(1, 1);
  size_t ol = 0, oc = 1, el = 0, ec = 1;
  int out_open = 1, err_open = 1, child_done = 0, st = 0, read_failed = 0;
  struct timespec start;
  clock_gettime(CLOCK_MONOTONIC, &start);
  const int limit = timeout_sec();
  while (out_open || err_open || !child_done) {
    if (!child_done) {
      pid_t waited = waitpid(pid, &st, WNOHANG);
      if (waited == pid) child_done = 1;
      else if (waited < 0 && errno != EINTR) { close(outp[0]); close(errp[0]); return -1; }
    }
    if (child_done && !out_open && !err_open) break;
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    int elapsed = (int)(now.tv_sec - start.tv_sec);
    if (elapsed >= limit) {
      kill(-pid, SIGKILL);
      if (!child_done) waitpid(pid, &cap->status, 0);
      cap->status = -1;
      close(outp[0]);
      close(errp[0]);
      return -1;
    }
    struct pollfd fds[2] = {
        {out_open ? outp[0] : -1, POLLIN, 0},
        {err_open ? errp[0] : -1, POLLIN, 0},
    };
    poll(fds, 2, 200);
    if (out_open) {
      int rc = append_fd(&cap->out, &ol, &oc, outp[0], 8 * 1024 * 1024);
      if (rc == 1) out_open = 0;
      if (rc < 0) {
        read_failed = 1;
        kill(-pid, SIGKILL);
        break;
      }
    }
    if (err_open) {
      int rc = append_fd(&cap->err, &el, &ec, errp[0], 1024 * 1024);
      if (rc == 1) err_open = 0;
      if (rc < 0) {
        read_failed = 1;
        kill(-pid, SIGKILL);
        break;
      }
    }
  }
  close(outp[0]);
  close(errp[0]);
  if (!child_done && waitpid(pid, &st, 0) < 0) return -1;
  cap->status = WIFEXITED(st) ? WEXITSTATUS(st) : 1;
  return read_failed ? -1 : 0;
}

const char *arg_str(const cJSON *args, const char *key) {
  if (!args) return NULL;
  const cJSON *item = cJSON_GetObjectItemCaseSensitive(args, key);
  return cJSON_IsString(item) ? item->valuestring : NULL;
}

int arg_int(const cJSON *args, const char *key, int fallback) {
  if (!args) return fallback;
  const cJSON *item = cJSON_GetObjectItemCaseSensitive(args, key);
  return cJSON_IsNumber(item) ? item->valueint : fallback;
}

int arg_bool(const cJSON *args, const char *key) {
  if (!args) return 0;
  const cJSON *item = cJSON_GetObjectItemCaseSensitive(args, key);
  return cJSON_IsTrue(item);
}

const cJSON *arg_array(const cJSON *args, const char *key) {
  if (!args) return NULL;
  const cJSON *item = cJSON_GetObjectItemCaseSensitive(args, key);
  return cJSON_IsArray(item) ? item : NULL;
}
