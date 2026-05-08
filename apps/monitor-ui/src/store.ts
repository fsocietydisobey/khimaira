import { configureStore } from "@reduxjs/toolkit";
import { setupListeners } from "@reduxjs/toolkit/query";

import { monitorApi } from "./api";

export const store = configureStore({
  reducer: {
    [monitorApi.reducerPath]: monitorApi.reducer,
  },
  middleware: (getDefault) => getDefault().concat(monitorApi.middleware),
});

setupListeners(store.dispatch);

export type RootState = ReturnType<typeof store.getState>;
export type AppDispatch = typeof store.dispatch;
