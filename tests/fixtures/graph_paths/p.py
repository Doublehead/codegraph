# Long class is declared FIRST so its save/load are the first candidate pair the
# resolver iterates. A first-pair-wins path() returns the 3-hop Long route; the fix
# must return the globally shortest, the 1-hop Short route.
class Long:
    def save(self):
        helper1()

    def load(self):
        pass


def helper1():
    helper2()


def helper2():
    x = Long()
    x.load()        # Long.save -> helper1 -> helper2 -> Long.load  (3 hops)


class Short:
    def save(self):
        self.load()  # Short.save -> Short.load  (1 hop)

    def load(self):
        pass
