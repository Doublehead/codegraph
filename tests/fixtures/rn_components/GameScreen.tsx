import { apiUrl } from "./apiConfig";

export const GameScreen = (props) => {
  const u = apiUrl("/x");            // Gap 3: cross-file .tsx -> .ts call
  return <DungeonView room={u} />;   // Gap 2: JSX child -> Gap 1: HOC component
};
