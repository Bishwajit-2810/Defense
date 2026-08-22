import { describe, it, expect } from 'vitest';
import { extractSystemMetrics } from './useSystemMetrics';

describe('extractSystemMetrics', () => {
  it('extracts host telemetry with discrete GPU correctly', () => {
    const mockData = {
      status: 'healthy',
      host: {
        cpu: { percent: 15.6 },
        memory: { percent: 42.8, used_human: '13.5 GB', total_human: '31.1 GB' },
        gpu: {
          available: true,
          discrete: [{ utilization_gpu_percent: 10.4 }],
        },
      },
    };

    const res = extractSystemMetrics(mockData);
    expect(res.cpu).toBe(15.6);
    expect(res.gpu).toBe(10.4);
    expect(res.gpuAvailable).toBe(true);
    expect(res.ram).toBe(42.8);
    expect(res.ramUsed).toBe('13.5 GB');
    expect(res.status).toBe('healthy');
  });

  it('handles integrated GPU when discrete is absent', () => {
    const mockData = {
      status: 'healthy',
      host: {
        cpu: { percent: 5.0 },
        memory: { percent: 30.0 },
        gpu: {
          available: true,
          discrete: [],
          integrated: [{ utilization_percent: 8.5 }],
        },
      },
    };

    const res = extractSystemMetrics(mockData);
    expect(res.cpu).toBe(5.0);
    expect(res.gpu).toBe(8.5);
    expect(res.gpuAvailable).toBe(true);
  });

  it('handles missing or empty data gracefully', () => {
    const res = extractSystemMetrics(null);
    expect(res.cpu).toBe(0);
    expect(res.gpu).toBeNull();
    expect(res.gpuAvailable).toBe(false);
    expect(res.ram).toBe(0);
    expect(res.status).toBe('offline');
  });
});
