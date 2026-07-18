class Runner:
    def go(self): return 1


class Task:
    def go(self): return 1


def runner_caller():
    r = Runner()
    return r.go()


def task_caller():
    t = Task()
    return t.go()
