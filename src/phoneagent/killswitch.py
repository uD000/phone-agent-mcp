"""Human kill switch, independent of the agent: create ~/.phone-agent/KILL (pa-kill) and every input stops at once;
remove it (pa-resume) to continue. The agent cannot clear it."""
from .services.common import HOME

FLAG = HOME / 'KILL'


def is_on():
    return FLAG.exists()
