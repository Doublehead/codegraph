export class Base {
  greet(): string { return this.format(); }
  format(): string { return "x"; }
}
export class Child extends Base {
  greet(): string { return super.greet(); }
}
function topLevel(): number { return helper(); }
function helper(): number { return 1; }
