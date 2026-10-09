"use client";

import { useEffect, useRef, useState } from "react";

/** A portrait viewport that rotates and fits the whole image without cropping. */
export default function PhotoFrame({ src, alt, rotation, onRotationResolved, loading }: {
  src: string; alt: string; rotation?: number | null;
  onRotationResolved?: (angle: number) => void; loading?: "lazy" | "eager";
}) {
  const frame = useRef<HTMLSpanElement>(null);
  const [box, setBox] = useState({ width: 0, height: 0 });
  const [image, setImage] = useState({ width: 1, height: 1 });
  useEffect(() => {
    if (!frame.current) return;
    const observer = new ResizeObserver(([entry]) => setBox({
      width: entry.contentRect.width, height: entry.contentRect.height,
    }));
    observer.observe(frame.current);
    return () => observer.disconnect();
  }, []);
  const angle = rotation ?? (image.width > image.height ? 90 : 0);
  const sideways = angle === 90 || angle === 270;
  const scale = Math.min(box.width / (sideways ? image.height : image.width),
    box.height / (sideways ? image.width : image.height));
  return <span className="capture-photo-frame" ref={frame}>
    {/* Photos use same-origin auth; retain the original and do not crop the card. */}
    {/* eslint-disable-next-line @next/next/no-img-element */}
    <img src={src} alt={alt} decoding="async" loading={loading} onLoad={event => {
      const { naturalWidth: width, naturalHeight: height } = event.currentTarget;
      setImage({ width, height });
      onRotationResolved?.(rotation ?? (width > height ? 90 : 0));
    }} style={{ width: image.width * scale, height: image.height * scale,
      transform: `translate(-50%, -50%) rotate(${angle}deg)` }} />
  </span>;
}
