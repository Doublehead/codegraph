import React from "react";

export const DungeonView = React.memo((props) => {   // Gap 1: HOC-wrapped component
  return <view>{props.room}</view>;
});
