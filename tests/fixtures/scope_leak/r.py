class A:
    def m(self): pass


class B:
    def m(self): pass


def reassigned():
    x = A()
    x.m()
    x = B()
    x.m()   # x assigned two different classes -> conflict -> ambiguous, never wrong-exact
