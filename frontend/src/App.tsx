import { Route, Routes } from "react-router-dom";

import { DeskProvider, Shell } from "./desk";
import { Chat } from "./pages/Chat";
import { Contracts } from "./pages/Contracts";
import { MemoryPage } from "./pages/Memory";
import { MonitorPage } from "./pages/Monitor";
import { Orders } from "./pages/Orders";
import { Products } from "./pages/Products";
import { Reports } from "./pages/Reports";

export function App() {
  return (
    <DeskProvider>
      <Shell>
        <Routes>
          <Route path="/" element={<Chat />} />
          <Route path="/products" element={<Products />} />
          <Route path="/orders" element={<Orders />} />
          <Route path="/contracts" element={<Contracts />} />
          <Route path="/reports" element={<Reports />} />
          <Route path="/memory" element={<MemoryPage />} />
          <Route path="/monitor" element={<MonitorPage />} />
        </Routes>
      </Shell>
    </DeskProvider>
  );
}
