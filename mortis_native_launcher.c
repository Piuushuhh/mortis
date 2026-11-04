#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shellapi.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <wchar.h>

#define COMMAND_CAPACITY 32768

static void log_event(const wchar_t *event, DWORD error_code) {
    wchar_t local_app_data[MAX_PATH];
    DWORD length = GetEnvironmentVariableW(L"LOCALAPPDATA", local_app_data, MAX_PATH);
    if (length == 0 || length >= MAX_PATH) return;

    wchar_t directory[MAX_PATH];
    if (swprintf(directory, MAX_PATH, L"%ls\\Mortis", local_app_data) < 0) return;
    CreateDirectoryW(directory, NULL);

    wchar_t path[MAX_PATH];
    if (swprintf(path, MAX_PATH, L"%ls\\native_host.log", directory) < 0) return;
    HANDLE file = CreateFileW(path, FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE,
                              NULL, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (file == INVALID_HANDLE_VALUE) return;

    SYSTEMTIME now;
    GetLocalTime(&now);
    wchar_t line[1024];
    int chars = swprintf(line, 1024,
        L"%04u-%02u-%02u %02u:%02u:%02u [native-launcher] %ls pid=%lu detail=%lu\r\n",
        now.wYear, now.wMonth, now.wDay, now.wHour, now.wMinute, now.wSecond,
        event, GetCurrentProcessId(), error_code);
    if (chars > 0 && chars < 1024) {
        char utf8[4096];
        int bytes = WideCharToMultiByte(CP_UTF8, 0, line, chars, utf8, sizeof(utf8), NULL, NULL);
        if (bytes > 0) {
            DWORD written = 0;
            WriteFile(file, utf8, (DWORD)bytes, &written, NULL);
        }
    }
    CloseHandle(file);
}

static bool append_char(wchar_t *buffer, size_t capacity, size_t *used, wchar_t ch) {
    if (*used + 1 >= capacity) return false;
    buffer[(*used)++] = ch;
    buffer[*used] = L'\0';
    return true;
}

/* Quote one argument using the Windows CRT command-line parsing rules. */
static bool append_quoted(wchar_t *buffer, size_t capacity, size_t *used,
                          const wchar_t *argument) {
    if (!append_char(buffer, capacity, used, L'"')) return false;
    size_t slashes = 0;
    for (const wchar_t *cursor = argument; ; ++cursor) {
        wchar_t ch = *cursor;
        if (ch == L'\\') {
            ++slashes;
            continue;
        }
        if (ch == L'"') {
            for (size_t i = 0; i < slashes * 2 + 1; ++i)
                if (!append_char(buffer, capacity, used, L'\\')) return false;
            if (!append_char(buffer, capacity, used, L'"')) return false;
            slashes = 0;
            continue;
        }
        if (ch == L'\0') {
            for (size_t i = 0; i < slashes * 2; ++i)
                if (!append_char(buffer, capacity, used, L'\\')) return false;
            return append_char(buffer, capacity, used, L'"');
        }
        for (size_t i = 0; i < slashes; ++i)
            if (!append_char(buffer, capacity, used, L'\\')) return false;
        slashes = 0;
        if (!append_char(buffer, capacity, used, ch)) return false;
    }
}

static bool append_text(wchar_t *buffer, size_t capacity, size_t *used,
                        const wchar_t *text) {
    while (*text) {
        if (!append_char(buffer, capacity, used, *text++)) return false;
    }
    return true;
}

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, PWSTR command_line, int show) {
    (void)instance;
    (void)previous;
    (void)command_line;
    (void)show;

    wchar_t module_path[32768];
    DWORD module_length = GetModuleFileNameW(NULL, module_path, 32768);
    if (module_length == 0 || module_length >= 32768) {
        log_event(L"Could not resolve launcher path", GetLastError());
        return 2;
    }
    wchar_t *separator = wcsrchr(module_path, L'\\');
    if (!separator) {
        log_event(L"Launcher path has no directory", ERROR_BAD_PATHNAME);
        return 2;
    }
    *separator = L'\0';

    wchar_t python_path[32768];
    wchar_t script_path[32768];
    if (swprintf(python_path, 32768, L"%ls\\.venv\\Scripts\\python.exe", module_path) < 0 ||
        swprintf(script_path, 32768, L"%ls\\mortis_native_host.py", module_path) < 0) {
        log_event(L"Host path is too long", ERROR_BUFFER_OVERFLOW);
        return 2;
    }
    if (GetFileAttributesW(python_path) == INVALID_FILE_ATTRIBUTES ||
        GetFileAttributesW(script_path) == INVALID_FILE_ATTRIBUTES) {
        log_event(L"Python interpreter or host script is missing", GetLastError());
        return 2;
    }

    int argc = 0;
    LPWSTR *argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    if (!argv) {
        log_event(L"Could not parse Native Messaging arguments", GetLastError());
        return 2;
    }

    wchar_t *child_command = (wchar_t *)calloc(COMMAND_CAPACITY, sizeof(wchar_t));
    if (!child_command) {
        LocalFree(argv);
        log_event(L"Could not allocate child command line", ERROR_NOT_ENOUGH_MEMORY);
        return 2;
    }
    size_t used = 0;
    bool valid = append_quoted(child_command, COMMAND_CAPACITY, &used, python_path) &&
        append_text(child_command, COMMAND_CAPACITY, &used, L" -u ") &&
        append_quoted(child_command, COMMAND_CAPACITY, &used, script_path);
    for (int i = 1; valid && i < argc; ++i) {
        valid = append_char(child_command, COMMAND_CAPACITY, &used, L' ') &&
            append_quoted(child_command, COMMAND_CAPACITY, &used, argv[i]);
    }
    LocalFree(argv);
    if (!valid) {
        free(child_command);
        log_event(L"Native Messaging command line exceeds the Windows limit", ERROR_BUFFER_OVERFLOW);
        return 2;
    }

    STARTUPINFOW startup;
    ZeroMemory(&startup, sizeof(startup));
    startup.cb = sizeof(startup);
    startup.dwFlags = STARTF_USESTDHANDLES;
    startup.hStdInput = GetStdHandle(STD_INPUT_HANDLE);
    startup.hStdOutput = GetStdHandle(STD_OUTPUT_HANDLE);
    startup.hStdError = GetStdHandle(STD_ERROR_HANDLE);

    PROCESS_INFORMATION process;
    ZeroMemory(&process, sizeof(process));
    BOOL created = CreateProcessW(python_path, child_command, NULL, NULL, TRUE,
        CREATE_NO_WINDOW, NULL, module_path, &startup, &process);
    free(child_command);
    if (!created) {
        log_event(L"Could not start the existing virtual-environment Python", GetLastError());
        return 2;
    }

    log_event(L"Python child started", process.dwProcessId);
    DWORD wait_result = WaitForSingleObject(process.hProcess, INFINITE);
    DWORD exit_code = 2;
    if (wait_result == WAIT_OBJECT_0 && !GetExitCodeProcess(process.hProcess, &exit_code))
        exit_code = 2;
    else if (wait_result != WAIT_OBJECT_0)
        log_event(L"Waiting for Python child failed", GetLastError());
    CloseHandle(process.hThread);
    CloseHandle(process.hProcess);
    return (int)exit_code;
}
