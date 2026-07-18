import { save } from './mod.js';
function run() { return save(); }
const obj = { handle() { return run(); } };
