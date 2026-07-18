db = Database()              # module-level construct


class Database:
    def query(self): pass


class FakeDB:
    def query(self): pass


class session:              # class name that collides with a common variable name
    def commit(self): pass


class DBSession:
    def commit(self): pass


class config:
    def run(self): pass


class Other:
    def run(self): pass


def handler(db):            # D1: untyped param shadows module `db` -> ambiguous (both), no wrong-exact
    db.query()


def use_global():           # D1 control: genuine module-global ref -> exact
    db.query()


def hinted(session: DBSession):   # D2: param hint shadows class `session` -> inferred to DBSession
    session.commit()


def constructed():          # D3: local construct shadows class `config` -> exact to Other
    config = Other()
    config.run()
