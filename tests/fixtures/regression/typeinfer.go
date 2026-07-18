package main

func useShort() int {
    r := Repo{}
    return r.Save()
}

func usePointer() int {
    p := &Repo{}
    return p.Save()
}

func useVar() int {
    var v Repo
    return v.Save()
}

type Repo struct{}

func (r Repo) Save() int { return 1 }

type Other struct{}

func (o Other) Save() int { return 2 }
