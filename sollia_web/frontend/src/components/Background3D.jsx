import { useRef, useMemo } from 'react'
import { Canvas, useFrame } from '@react-three/fiber'

function Particles({ count = 250 }) {
  const mesh = useRef()
  
  const positions = useMemo(() => {
    const pos = new Float32Array(count * 3)
    for (let i = 0; i < count; i++) {
      pos[i * 3] = (Math.random() - 0.5) * 20
      pos[i * 3 + 1] = (Math.random() - 0.5) * 12
      pos[i * 3 + 2] = (Math.random() - 0.5) * 8
    }
    return pos
  }, [count])

  const colors = useMemo(() => {
    const cols = new Float32Array(count * 3)
    for (let i = 0; i < count; i++) {
      const mix = Math.random()
      cols[i * 3] = 0.2 + mix * 0.1
      cols[i * 3 + 1] = 0.4 + mix * 0.2
      cols[i * 3 + 2] = 0.8 + mix * 0.2
    }
    return cols
  }, [count])

  useFrame((state) => {
    if (mesh.current) {
      mesh.current.rotation.x = state.clock.elapsedTime * 0.015
      mesh.current.rotation.y = state.clock.elapsedTime * 0.02
      mesh.current.position.z = Math.sin(state.clock.elapsedTime * 0.3) * 0.5
    }
  })

  return (
    <points ref={mesh}>
      <bufferGeometry>
        <bufferAttribute attach="attributes-position" count={count} array={positions} itemSize={3} />
        <bufferAttribute attach="attributes-color" count={count} array={colors} itemSize={3} />
      </bufferGeometry>
      <pointsMaterial size={0.04} vertexColors sizeAttenuation transparent opacity={0.5} blending={2} depthWrite={false} />
    </points>
  )
}

export default function Background3D() {
  return (
    <div style={{ position: 'fixed', inset: 0, zIndex: 0, pointerEvents: 'none' }}>
      <Canvas camera={{ position: [0, 0, 6], fov: 55 }} gl={{ antialias: true, alpha: true }}>
        <Particles count={300} />
      </Canvas>
    </div>
  )
}
