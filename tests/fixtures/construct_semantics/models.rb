class User
  def save; end
end

class Account
  def save; end
end

def non_new
  rows = User.where(1)   # not .new -> not construction -> ambiguous
  rows.save
end

def with_new
  u = User.new           # .new -> proven construction -> exact
  u.save
end
