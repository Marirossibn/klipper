#ifndef __GENERIC_MISC_H
#define __GENERIC_MISC_H

#include <stdarg.h> // va_list
#include <stddef.h> // size_t
#include <stdint.h> // uint8_t

struct command_encoder;
void console_sendf(const struct command_encoder *ce, va_list args);

uint32_t timer_from_us(uint32_t us);
uint8_t timer_is_before(uint32_t time1, uint32_t time2);
uint32_t timer_read_time(void);
void timer_kick(void);

void *dynmem_start(void);
void *dynmem_end(void);

uint16_t crc16_ccitt(char *buf, uint8_t len);

#endif // misc.h
