/* OTT v1, C17. Read one observation line; print one legal action line.
 * The baseline chooses the first legal action. Replace choose() for your strategy.
 * Keep stdout reserved for actions. Write diagnostics to stderr.
 */
#include <stdio.h>
#include <string.h>

static char observation[1048578];

static int choose(const char *json, const char **start, size_t *length) {
    const char *key = strstr(json, "\"legal_actions\"");
    if (!key) return 0;
    const char *array = strchr(key + strlen("\"legal_actions\""), '[');
    if (!array) return 0;
    const char *begin = array + 1;
    while (*begin == ' ' || *begin == '\t' || *begin == '\r') ++begin;
    if (*begin != '{') return 0;
    int depth = 0, quoted = 0, escaped = 0;
    for (const char *p = begin; *p; ++p) {
        if (quoted) {
            if (escaped) escaped = 0;
            else if (*p == '\\') escaped = 1;
            else if (*p == '"') quoted = 0;
        } else if (*p == '"') quoted = 1;
        else if (*p == '{') ++depth;
        else if (*p == '}' && --depth == 0) {
            *start = begin;
            *length = (size_t)(p - begin + 1);
            return 1;
        }
    }
    return 0;
}

int main(void) {
    while (fgets(observation, sizeof observation, stdin)) {
        if (!strchr(observation, '\n')) return 1;
        const char *action;
        size_t length;
        if (!choose(observation, &action, &length)) return 1;
        if (fwrite(action, 1, length, stdout) != length || putchar('\n') == EOF || fflush(stdout)) return 1;
    }
    return ferror(stdin) ? 1 : 0;
}
