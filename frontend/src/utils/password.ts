export const PASSWORD_MIN_LENGTH = 10
export const PASSWORD_MAX_LENGTH = 1024
export const PASSWORD_POLICY_HINT = '至少 10 位，含大写字母、小写字母和特殊符号'

const uppercasePattern = /[A-Z]/
const lowercasePattern = /[a-z]/
const specialPattern = /[^A-Za-z0-9\s]/

export function passwordPolicyError(value: string): string | null {
  if (value.length < PASSWORD_MIN_LENGTH) return `密码至少 ${PASSWORD_MIN_LENGTH} 位`
  if (value.length > PASSWORD_MAX_LENGTH) return `密码不能超过 ${PASSWORD_MAX_LENGTH} 位`
  if (!uppercasePattern.test(value) || !lowercasePattern.test(value) || !specialPattern.test(value)) {
    return '密码必须同时包含大写字母、小写字母和特殊符号'
  }
  return null
}
