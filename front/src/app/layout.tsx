import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "NFreader V2",
  description: "Leitura e conferência documental para o PCM",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="pt-BR"><body>{children}</body></html>;
}
