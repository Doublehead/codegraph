def save
  write()
end
def write
  1
end
class Repo
  def store
    flush()
  end
  def flush
    save()
  end
  def bareflush
    flush
  end
end
module M
  def helper
    save()
  end
end
def loopa; loopb(); end
def loopb; loopa(); end
