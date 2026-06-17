import React, { Suspense, useRef } from "react";
import { Canvas, useFrame } from "@react-three/fiber";
import { useQuery } from "@tanstack/react-query";
import { queryFn } from "../api.js";

// A lightweight stand-in "brain": an icosahedron core wrapped in a wireframe
// shell with orbiting synapse nodes. Pure r3f/three primitives — no asset
// loading, so it builds and runs without external models.
function BrainMesh({ activity = 0.3 }) {
  const core = useRef();
  const shell = useRef();

  useFrame((state, delta) => {
    if (core.current) core.current.rotation.y += delta * 0.3;
    if (shell.current) {
      shell.current.rotation.y -= delta * 0.15;
      shell.current.rotation.x += delta * 0.05;
    }
  });

  const synapses = [];
  const count = 14;
  for (let i = 0; i < count; i++) {
    const t = (i / count) * Math.PI * 2;
    const r = 2.1;
    synapses.push([
      Math.cos(t) * r,
      Math.sin(t * 1.7) * 0.9,
      Math.sin(t) * r,
    ]);
  }

  const glow = 0.4 + Math.min(activity, 1) * 0.6;

  return (
    <group>
      <mesh ref={core}>
        <icosahedronGeometry args={[1.2, 1]} />
        <meshStandardMaterial
          color="#0ea5e9"
          emissive="#38bdf8"
          emissiveIntensity={glow}
          roughness={0.3}
          metalness={0.5}
        />
      </mesh>
      <mesh ref={shell}>
        <icosahedronGeometry args={[1.9, 1]} />
        <meshStandardMaterial color="#38bdf8" wireframe transparent opacity={0.25} />
      </mesh>
      {synapses.map((pos, i) => (
        <mesh key={i} position={pos}>
          <sphereGeometry args={[0.07, 12, 12]} />
          <meshStandardMaterial
            color="#7dd3fc"
            emissive="#7dd3fc"
            emissiveIntensity={glow}
          />
        </mesh>
      ))}
    </group>
  );
}

export default function Brain({ stream }) {
  // Drive the glow from recent knowledge/learning events if the API is present.
  const { data } = useQuery({
    queryKey: ["brain"],
    queryFn: queryFn("/brain"),
    retry: 0,
  });

  const learningEvents = (stream?.events || []).filter((e) =>
    ["KNOWLEDGE_UPDATED", "INSIGHT_PUBLISHED", "LESSON_CAPTURED"].includes(
      e.type
    )
  ).length;

  const activity =
    (data?.activity != null ? Number(data.activity) : 0) +
    Math.min(learningEvents / 20, 1);

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-xl font-semibold">Brain</h1>
        <p className="text-sm text-slate-500">
          Knowledge graph activity. Glow scales with learning events.
        </p>
      </header>

      <div className="card h-[480px] overflow-hidden p-0">
        <Canvas camera={{ position: [0, 0, 6], fov: 50 }}>
          <ambientLight intensity={0.4} />
          <pointLight position={[5, 5, 5]} intensity={1.2} />
          <pointLight position={[-5, -3, -5]} intensity={0.6} color="#38bdf8" />
          <Suspense fallback={null}>
            <BrainMesh activity={activity} />
          </Suspense>
        </Canvas>
      </div>

      <div className="grid grid-cols-3 gap-4">
        <div className="card">
          <div className="text-xs uppercase text-slate-500">Documents</div>
          <div className="mt-1 text-2xl font-semibold">
            {data?.documents ?? "—"}
          </div>
        </div>
        <div className="card">
          <div className="text-xs uppercase text-slate-500">Lessons</div>
          <div className="mt-1 text-2xl font-semibold">
            {data?.lessons ?? "—"}
          </div>
        </div>
        <div className="card">
          <div className="text-xs uppercase text-slate-500">
            Learning events
          </div>
          <div className="mt-1 text-2xl font-semibold">{learningEvents}</div>
        </div>
      </div>
    </div>
  );
}
