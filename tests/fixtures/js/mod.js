function save() { return write(); }
function write() { return 1; }
const boot = () => save();
class Repo {
  store() { return this.flush(); }
  flush() { return save(); }
}
function loopA() { return loopB(); }
function loopB() { return loopA(); }
