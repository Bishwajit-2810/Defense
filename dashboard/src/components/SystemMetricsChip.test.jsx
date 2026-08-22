import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import SystemMetricsChip from './SystemMetricsChip';

const mockApiCall = vi.fn();
vi.mock('../utils/api', () => ({
  apiCall: (...args) => mockApiCall(...args),
  API_BASE: 'http://127.0.0.1:8001',
  getSseQueryAsync: vi.fn().mockResolvedValue('?ticket=test'),
}));

describe('SystemMetricsChip', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders vertical chip on the right side with custom CPU, GPU, and RAM telemetry usages', () => {
    const customMetrics = {
      cpu: 14.2,
      gpu: 35.0,
      gpuAvailable: true,
      ram: 48.6,
      ramUsed: '15.2 GB',
      ramTotal: '31.1 GB',
      status: 'healthy',
    };

    render(
      <SystemMetricsChip
        orientation="vertical"
        onOpen={() => {}}
        customMetrics={customMetrics}
      />
    );

    const verticalChip = screen.getByTestId('system-metrics-chip-vertical');
    expect(verticalChip).toBeInTheDocument();
    expect(verticalChip.className).toContain('fixed right-3');

    expect(screen.getByTestId('chip-cpu')).toHaveTextContent('CPU14%');
    expect(screen.getByTestId('chip-gpu')).toHaveTextContent('GPU35%');
    expect(screen.getByTestId('chip-ram')).toHaveTextContent('RAM49%');
    expect(screen.getByText('Alt+M')).toBeInTheDocument();
  });

  it('renders horizontal chip when orientation is horizontal', () => {
    const customMetrics = {
      cpu: 12.0,
      gpu: 20.0,
      gpuAvailable: true,
      ram: 50.0,
    };

    render(
      <SystemMetricsChip
        orientation="horizontal"
        onOpen={() => {}}
        customMetrics={customMetrics}
      />
    );

    expect(screen.getByTestId('system-metrics-chip-horizontal')).toBeInTheDocument();
  });

  it('handles N/A when GPU is not available', () => {
    const customMetrics = {
      cpu: 8.0,
      gpu: null,
      gpuAvailable: false,
      ram: 32.0,
    };

    render(<SystemMetricsChip orientation="vertical" onOpen={() => {}} customMetrics={customMetrics} />);

    expect(screen.getByTestId('chip-gpu')).toHaveTextContent('GPUN/A');
  });

  it('triggers onOpen when clicked', () => {
    const handleOpen = vi.fn();
    const customMetrics = {
      cpu: 10,
      gpu: 0,
      gpuAvailable: true,
      ram: 40,
    };

    render(<SystemMetricsChip orientation="vertical" onOpen={handleOpen} customMetrics={customMetrics} />);

    const chip = screen.getByRole('button');
    fireEvent.click(chip);

    expect(handleOpen).toHaveBeenCalledTimes(1);
  });

  it('triggers onOpen when Enter or Space key is pressed', () => {
    const handleOpen = vi.fn();
    const customMetrics = {
      cpu: 12,
      gpu: 5,
      gpuAvailable: true,
      ram: 45,
    };

    render(<SystemMetricsChip orientation="vertical" onOpen={handleOpen} customMetrics={customMetrics} />);

    const chip = screen.getByRole('button');
    fireEvent.keyDown(chip, { key: 'Enter' });
    fireEvent.keyDown(chip, { key: ' ' });

    expect(handleOpen).toHaveBeenCalledTimes(2);
  });

  it('loads live metrics via useSystemMetrics when no customMetrics are passed', async () => {
    mockApiCall.mockResolvedValueOnce({
      host: {
        cpu: { percent: 18.5 },
        memory: { percent: 52.1, used_human: '16.0 GB', total_human: '31.1 GB' },
        gpu: {
          available: true,
          discrete: [{ utilization_gpu_percent: 22.0 }],
        },
      },
    });

    render(<SystemMetricsChip orientation="vertical" onOpen={() => {}} />);

    expect(mockApiCall).toHaveBeenCalledWith('/v1/system/stats');
    expect(await screen.findByText('19%')).toBeInTheDocument();
    expect(await screen.findByText('22%')).toBeInTheDocument();
    expect(await screen.findByText('52%')).toBeInTheDocument();
  });
});
