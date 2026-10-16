import { useEffect, useState } from "react";
import { prefersDark } from "./theme";

export function useDark(): boolean {
  const [dark, setDark] = useState(prefersDark());
  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const on = () => setDark(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return dark;
}
