package main
func save() int { return write() }
func write() int { return 1 }
type Repo struct{}
func (r Repo) Store() int { return r.Flush() }
func (r Repo) Flush() int { return save() }
func loopa() int { return loopb() }
func loopb() int { return loopa() }
