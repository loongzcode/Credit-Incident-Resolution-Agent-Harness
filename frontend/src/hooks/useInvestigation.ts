import { useQuery } from '@tanstack/react-query';
import { caseApi } from '../api/caseApi';

export function useInvestigation(caseId: string, polling: boolean) {
  const options = { refetchInterval: polling ? 5000 : false as const, retry: false, refetchOnWindowFocus: false };
  const caseQuery = useQuery({ queryKey: ['case', caseId], queryFn: ({ signal }) => caseApi.case(caseId, signal), ...options });
  const evidence = useQuery({ queryKey: ['evidence', caseId], queryFn: ({ signal }) => caseApi.evidence(caseId, signal), ...options });
  const graph = useQuery({ queryKey: ['graph', caseId], queryFn: ({ signal }) => caseApi.graph(caseId, signal), ...options });
  const context = useQuery({ queryKey: ['context', caseId], queryFn: ({ signal }) => caseApi.context(caseId, signal), ...options });
  const queries = [caseQuery, evidence, graph, context];
  return { caseQuery, evidence, graph, context, refreshing: queries.some(q => q.isFetching),
    refresh: () => Promise.all(queries.map(q => q.refetch())) };
}
