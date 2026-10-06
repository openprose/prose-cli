import { failure } from "../core/errors";
import transportLimits from "../../../shared/capabilities/transport-limits.v1.json";
export const DEFAULT_NATIVE_OUTPUT_BYTES = 67_108_864;
type Selection = { nativeOutputBytes?: string; outputContract?: string; nativeLog?: string };
export function validateNativeOutputBytes(value: string): number {
 const n=Number(value);
 if(!/^[1-9][0-9]*$/.test(value)||!Number.isSafeInteger(n)||n<1_048_576||n>268_435_456) throw failure("CONFIG_INVALID",{reason:"Native output bytes must be decimal bytes from 1048576 through 268435456."});
 return n;
}
export function nativeOutputBytes(input: Selection): number { return input.nativeOutputBytes===undefined?DEFAULT_NATIVE_OUTPUT_BYTES:validateNativeOutputBytes(input.nativeOutputBytes); }
export function nativeOutputLimits(input: Selection): {maxRecordBytes:number;maxAggregateStdoutBytes:number;maxNativeCaptureBytes:number;captureEnabled:boolean}|undefined {
 if(input.nativeOutputBytes!==undefined&&input.outputContract!=="native")throw failure("CONFIG_INVALID",{reason:"Native output bytes require native output mode."});
 return input.outputContract==="native"?{maxRecordBytes:transportLimits.maxRecordBytes,maxAggregateStdoutBytes:nativeOutputBytes(input),maxNativeCaptureBytes:nativeOutputBytes(input),captureEnabled:input.nativeLog!==undefined}:undefined;
}
