def helper
  puts "free"
end

def other_fn
end

module Util
  def helper
    puts "mixin"
  end
end

class Worker
  include Util
  def run
    helper
  end
end

class Plain
  def go
    helper
  end
end

class Fallback
  include Util
  def go2
    other_fn
  end
end
