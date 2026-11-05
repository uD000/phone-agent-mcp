"""Out-of-band human approval: a native macOS dialog. The agent cannot click it."""
import subprocess


def _quote(text):
    return '"' + text.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n') + '"'


def ask(title, message, allow='Allow once', timeout=120):
    """True only if the human clicked the allow button. Enter, Escape, closing or timing out all mean no."""
    script = ('set r to display dialog %s with title %s with icon caution buttons {"Deny", %s} default button "Deny" '
              'cancel button "Deny" giving up after %d\nif gave up of r then return "TIMEOUT"\nreturn button returned of r'
              ) % (_quote(message), _quote('phone-agent: ' + title), _quote(allow), timeout)
    try:
        result = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=timeout + 15)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == allow
