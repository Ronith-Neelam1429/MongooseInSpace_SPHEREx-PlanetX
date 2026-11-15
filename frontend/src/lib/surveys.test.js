import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { retrying } from './surveys.js';

describe('retrying the sky imagery', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('tries again after each wait and stops at the first success', async () => {
    const task = vi.fn()
      .mockRejectedValueOnce(new Error('502'))
      .mockRejectedValueOnce(new Error('503'))
      .mockResolvedValue('https://sky.test');
    const onSuccess = vi.fn(), onFailure = vi.fn();
    retrying(task, [2000, 5000, 10000], { onSuccess, onFailure });
    await vi.advanceTimersByTimeAsync(0);
    expect(task).toHaveBeenCalledTimes(1);
    expect(onFailure).toHaveBeenLastCalledWith(new Error('502'), true);
    await vi.advanceTimersByTimeAsync(1999);
    expect(task).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(task).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(5000);
    expect(task).toHaveBeenCalledTimes(3);
    expect(onSuccess).toHaveBeenCalledWith('https://sky.test');
    await vi.advanceTimersByTimeAsync(60000);
    expect(task).toHaveBeenCalledTimes(3);
  });

  it('gives up after the last wait and says so', async () => {
    const task = vi.fn().mockRejectedValue(new Error('down'));
    const onFailure = vi.fn();
    retrying(task, [2000, 5000, 10000], { onSuccess: vi.fn(), onFailure });
    await vi.advanceTimersByTimeAsync(20000);
    expect(task).toHaveBeenCalledTimes(4);
    expect(onFailure.mock.calls.map(([, again]) => again)).toEqual([true, true, true, false]);
  });

  it('runs nothing once cancelled', async () => {
    const task = vi.fn().mockRejectedValue(new Error('down'));
    const onFailure = vi.fn(), onSuccess = vi.fn();
    const cancel = retrying(task, [2000], { onSuccess, onFailure });
    await vi.advanceTimersByTimeAsync(0);
    cancel();
    await vi.advanceTimersByTimeAsync(10000);
    expect(task).toHaveBeenCalledTimes(1);
    expect(onFailure).toHaveBeenCalledTimes(1);
  });
});
