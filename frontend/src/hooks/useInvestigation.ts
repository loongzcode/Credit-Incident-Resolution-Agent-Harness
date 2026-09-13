import { useQuery } from '@tanstack/react-query';
import { caseApi } from '../api/caseApi';

export function useInvestigation(caseId: string, polling: boolean) {
  return useQuery({ queryKey: ['investigation-frame', caseId],
    queryFn: ({ signal }) => caseApi.frame(caseId, signal),
    refetchInterval: polling ? 5000 : false, retry: false, refetchOnWindowFocus: false });
}
