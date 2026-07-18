class Svc { do() {} }
class Svc2 { do() {} }            // decoy

function handler(s: Svc) {        // typed param -> inferred edge to Svc.do
    s.do();
}
